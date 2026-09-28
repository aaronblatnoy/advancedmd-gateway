# 2026-09-28: Portal recovery decided by System One (Noul, then Choice)

## Context

When a deterministic stage hit a state outside its script, the recovery
node asked a local chat model (llama/qwen via llm-server) to emit a tool
call over the control outline. That is generation used for a selection
problem: slow (seconds), parse-dependent, and able to name a control that
is not in the outline.

## Decision (owner: "noul decides whether there is a failure. choice
decides what to do to get back on track.")

- Noul first: "is this a recoverable UI blocker one listed action could
  clear?" Below 0.60 the loop aborts immediately, which also replaces the
  guess that a slow page is an expired session.
- Choice second: one action from the observed refs (dialog and close
  controls ranked first, cap 40) plus Escape, Enter, none. Floor 0.25; none must carry less mass than the pick.
- Both go to Winnow on s1-server because labels are PHI. Hosted Jev never
  sees the outline.
- Deterministic goal probes still run between steps; step cap and history
  stay in code. Ollama path kept behind PORTAL_RECOVERY_DECIDER=ollama.
- Applies to every computer-use tool: all enter recovery via run_flow or
  the insurance graph. The same design is ported to availity-mcp.
