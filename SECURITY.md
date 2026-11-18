# Security

GuardCI is a research prototype, not a hardened multi-tenant service. Keep
untrusted authors separate from the owner's database, configuration, caches,
golden inputs and Docker access. Docker alone is not a complete sandbox.
Recipe execution is untrusted code execution; provision appropriate host-level
isolation and avoid sharing sensitive host resources.

Do not put API keys, SSH keys, private designs or confidential artifacts in Git
or bug reports. A passing check only establishes the declared checks and evidence
coverage, not safety outside that scope.

Report vulnerabilities privately using
[GitHub private vulnerability reporting](https://github.com/GuardCI-project/GuardCI/security/advisories/new).
Sign in to GitHub and include the affected version, impact and a minimal
reproduction without credentials or confidential design data. Do not post
exploitable details or sensitive reproductions in public issues.
No response SLA or hosted service is currently declared.
