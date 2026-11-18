# Security

GuardCI is a research prototype, not a hardened multi-tenant service. Keep
untrusted authors separate from the owner's database, configuration, caches,
golden inputs and Docker access. Docker alone is not a complete sandbox.
Recipe execution is untrusted code execution; provision appropriate host-level
isolation and avoid sharing sensitive host resources.

Do not put API keys, SSH keys, private designs or confidential artifacts in Git
or bug reports. A passing check only establishes the declared checks and evidence
coverage, not safety outside that scope.

Before publishing this repository, the maintainer must configure a private
vulnerability-reporting channel on the hosting service and document its location.
No security email address, hosted service or response SLA is currently declared.
Until a private channel exists, do not post exploitable details or sensitive
reproductions publicly.
