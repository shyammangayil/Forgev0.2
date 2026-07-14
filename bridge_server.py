import json, threading, uuid, subprocess, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

pending = {}
lock = threading.Lock()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _body(self):
        n = int(self.headers.get('Content-Length',0))
        return json.loads(self.rfile.read(n)) if n else {}
    def _send(self, code, data, ct='application/json'):
        body = (json.dumps(data) if ct=='application/json' else data).encode()
        self.send_response(code)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type,x-api-key,anthropic-version')
        self.end_headers()
        self.wfile.write(body)
    def do_OPTIONS(self):
        self.send_response(204)
        for h,v in [('Access-Control-Allow-Origin','*'),('Access-Control-Allow-Methods','GET,POST,OPTIONS'),('Access-Control-Allow-Headers','Content-Type,x-api-key,anthropic-version')]:
            self.send_header(h,v)
        self.end_headers()
    def do_GET(self):
        p = urlparse(self.path)
        qs = parse_qs(p.query)
        if p.path == '/bridge/status':
            self._send(200,{'status':'running','pending':len(pending)})
        elif p.path == '/bridge/pending':
            with lock:
                w = {rid:r['request'] for rid,r in pending.items() if r['response'] is None}
            self._send(200,{'requests':w})
        elif p.path == '/bridge/file':
            path = qs.get('path',[''])[0]
            offset = int(qs.get('offset',[0])[0])
            try:
                content = Path(path).read_text(errors='replace')
                self._send(200,{'content':content[offset:],'size':len(content)})
            except: self._send(200,{'content':'','size':0})
        else:
            self._send(404,{'error':'not found'})
    def do_POST(self):
        p = urlparse(self.path).path
        if p == '/v1/messages':
            req = self._body()
            rid = str(uuid.uuid4())[:8]
            evt = threading.Event()
            with lock:
                pending[rid] = {'request':req,'response':None,'event':evt}
            print(f'[BRIDGE] {rid} queued model={req.get("model")} msgs={len(req.get("messages",[]))}',flush=True)
            if evt.wait(timeout=180):
                with lock: resp = pending.pop(rid)['response']
                print(f'[BRIDGE] {rid} fulfilled',flush=True)
                self._send(200,resp)
            else:
                self._send(504,{'error':'timeout'})
        elif p == '/bridge/response':
            d = self._body()
            rid,resp = d.get('request_id'),d.get('response')
            with lock:
                if rid in pending:
                    pending[rid]['response']=resp; pending[rid]['event'].set()
                    self._send(200,{'ok':True})
                else: self._send(404,{'error':f'unknown {rid}'})
        elif p == '/bridge/run':
            d = self._body()
            script = d.get('script','/home/claude/forge/run_forge_test.py')
            def _run():
                log = open('/home/claude/forge/workspace/forge_pipeline.log','w')
                subprocess.run(['python3',script], stdout=log, stderr=log, cwd='/home/claude/forge')
                log.close()
            threading.Thread(target=_run,daemon=True).start()
            self._send(200,{'ok':True,'msg':'pipeline started'})
        else:
            self._send(404,{'error':'not found'})

Path('/home/claude/forge/workspace/logs').mkdir(parents=True,exist_ok=True)
Path('/home/claude/forge/workspace/TASKS').mkdir(parents=True,exist_ok=True)
Path('/home/claude/forge/workspace/dashboard').mkdir(parents=True,exist_ok=True)
server = HTTPServer(('0.0.0.0',9999),H)
print('FORGE Bridge running on :9999',flush=True)
server.serve_forever()
