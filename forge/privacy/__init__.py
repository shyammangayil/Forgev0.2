"""
FORGE Privacy Gateway package.

Public API::

    from forge.privacy.gateway import (
        PrivacyGateway,       # main gateway class
        get_privacy_gateway,  # module-level singleton
        PrivacyViolationError,# raised when gate=fail and HIGH findings present
        GatewayResult,        # result from scan_and_redact()
        PrivacyFinding,       # individual finding dataclass
        PiiDetector,          # PII detector (email, phone, SSN, CC, IBAN, ...)
        GdprClassifier,       # GDPR Article 9 special-category detector
        HipaaDetector,        # HIPAA PHI detector (MRN, NPI, DEA, ...)
        DataResidencyChecker, # Cross-border data residency checker
        PromptLeakDetector,   # Prompt injection / exfiltration detector
    )

Integration points:

  1. ``engine.create_task()``  — scans title, description, context, criteria
     at task creation.  Hard-blocks (deletes task + raises) when gate=fail.

  2. ``agents.BaseAgent._call_llm()`` — scans the assembled prompt before it
     is sent to any LLM provider, and the response before it is returned.

Environment variables:

  FORGE_PRIVACY_GATE        'warn' (default) | 'fail'
  FORGE_PRIVACY_REDACT      'true' (default) | 'false'
  FORGE_DATA_RESIDENCY      '' | 'EU' | 'US'
  FORGE_PRIVACY_LOG_LEVEL   'summary' (default) | 'full'
  FORGE_PRIVACY_ENABLED     'true' (default) | 'false'
"""
from forge.privacy.gateway import (
    PrivacyGateway,
    get_privacy_gateway,
    PrivacyViolationError,
    GatewayResult,
    PrivacyFinding,
    PiiDetector,
    GdprClassifier,
    HipaaDetector,
    DataResidencyChecker,
    PromptLeakDetector,
)

__all__ = [
    "PrivacyGateway",
    "get_privacy_gateway",
    "PrivacyViolationError",
    "GatewayResult",
    "PrivacyFinding",
    "PiiDetector",
    "GdprClassifier",
    "HipaaDetector",
    "DataResidencyChecker",
    "PromptLeakDetector",
]
