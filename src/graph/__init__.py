"""AgentCore Platform v1.0"""

# Package entry point for AgentRegistry manifest discovery.
# config/agent.yaml declares module="src.graph", class="FinancialContractReviewAgent",
# so the class MUST be importable from this package (not just src.graph.graph).
# server.py imports the Graph alias.

from src.graph.graph import FinancialContractReviewAgent, Graph

__all__ = ["FinancialContractReviewAgent", "Graph"]
