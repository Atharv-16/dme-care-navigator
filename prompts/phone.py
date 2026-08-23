"""Shared phone-call shape rules appended to every dialogue agent."""

CALLEE_RULES = (
    "PHONE CALL RULES: First line is a greeting only "
    "(your organization name + how can I help). "
    "Do not answer the request, quote inventory, or say [END] on that greeting. "
    "Wait for the caller. Answer only what they asked. "
    "When the call is done, one-sentence goodbye then [END]."
)

CALLER_RULES = (
    "PHONE CALL RULES: Wait for their greeting. Then identify yourself. "
    "Ask what you need. Do not [END] on your first speaking turn. "
    "When you have the answer (or they cannot help), thank them, say goodbye, [END]."
)

GOODBYE_FROM_CALLER = "The caller is wrapping up. One-sentence goodbye, then [END]."
GOODBYE_FROM_CALLEE = "They are saying goodbye. One-sentence goodbye, then [END]."
