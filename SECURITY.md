# Security

RAPP UltraCode treats model output, repository content, plans, tool results,
and generated patches as untrusted.

The v0.1 execution boundary is an isolated Git worktree with custom
path-confined tools. It is not an OS sandbox. Operator-declared checks can
execute repository code on the host and must be used only with trusted
repositories.

The runtime never exposes built-in shell, web, MCP, or Git tools to coding
agents. It never mutates the caller checkout, merges, pushes, opens pull
requests, installs packages, or falls back to arbitrary host execution.

Plans containing checks require the separate `--allow-host-checks` runtime
confirmation. Checks may execute model-modified repository code and are not an
OS sandbox; omit them unless the repository and declared command are trusted.

Report vulnerabilities privately to the repository owner. Do not publish
tokens, prompts containing confidential source, RDW transcripts, run
databases, or worktree artifacts.
