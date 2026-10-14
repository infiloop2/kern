"""Fixed, host-owned policy for reviewing held GitHub pushes."""
TOOL_ID = "github"
ACTION_ID = "push"
POLICY = """Review introduced .github/ changes for potentially malicious attempts to exploit GitHub Actions running on pull requests without human review. Look for credential or secret theft, exfiltration, and untrusted pull-request code gaining privileged execution or access. Approve changes without evidence of this abuse; otherwise leave the push for manual review. Referencing scripts or actions outside the supplied files is normal and is not, by itself, grounds to withhold approval. This is a review of the supplied .github/ changes, not a security audit of the entire repository."""
