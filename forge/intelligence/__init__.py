"""
FORGE Test & Repository Intelligence package.

Test Intelligence (static analysis of generated code):
    from forge.intelligence import (
        TestIntelligenceEngine,   # analyse(source) → TestIntelligenceReport
        TestIntelligenceReport,
        get_intelligence_engine,
    )

Repository Intelligence (reverse-engineer an existing codebase):
    from forge.intelligence import (
        RepositoryIntelligenceEngine,  # analyse(path) → RepositoryReport
        RepositoryReport,
        RepoComparisonReport,
        get_repo_engine,
    )
"""
from forge.intelligence.analyser import (
    TestIntelligenceEngine,
    TestIntelligenceReport,
    CriticalPath,
    DeadCodeBlock,
    RiskArea,
    UntestedArea,
    get_intelligence_engine,
)
from forge.intelligence.repository import (
    RepositoryIntelligenceEngine,
    RepositoryReport,
    RepoComparisonReport,
    ModuleInfo,
    PatternMatch,
    get_repo_engine,
)

__all__ = [
    # Test intelligence
    "TestIntelligenceEngine",
    "TestIntelligenceReport",
    "CriticalPath",
    "DeadCodeBlock",
    "RiskArea",
    "UntestedArea",
    "get_intelligence_engine",
    # Repository intelligence
    "RepositoryIntelligenceEngine",
    "RepositoryReport",
    "RepoComparisonReport",
    "ModuleInfo",
    "PatternMatch",
    "get_repo_engine",
]
