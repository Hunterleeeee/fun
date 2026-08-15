# Security Policy

Fun executes coding tools in a local workspace. Security issues can include workspace escape, arbitrary command execution outside policy, credential leakage, unsafe recovery that repeats side effects, or plugin bypass of the policy engine.

## Reporting

Please do not open a public issue for an unpatched vulnerability. Contact the repository maintainers privately through the security contact configured on GitHub, including:

- affected version or commit;
- operating system and Python version;
- minimal reproduction;
- expected and actual behavior;
- logs with credentials and private code removed.

We will acknowledge reports, validate the impact, and coordinate a fix and disclosure timeline with the reporter.

## Safe use

Do not provide production credentials to development builds. Review the workspace, approval mode, and diff before allowing changes. The alpha runtime is not a production sandbox.
