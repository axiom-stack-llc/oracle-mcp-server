"""CLI to generate opaque bearer tokens for the Axiom Oracle MCP server.

Usage:
    python -m mcp_server.scripts.generate_token            # one token
    python -m mcp_server.scripts.generate_token --count 5  # five tokens

Tokens are 32 bytes of cryptographically-random data, encoded as
URL-safe base64 (rstrip-padded). Each token is ~43 ASCII characters.

After generation, the operator adds the token(s) to the comma-separated
allowlist that backs the server's ``AXIOM_MCP_API_KEYS`` env var, then
restarts the server process so it picks up the new value. The exact
restart command depends on the deployment runtime (e.g.
``ecs update-service --force-new-deployment`` for AWS ECS,
``kubectl rollout restart`` for Kubernetes, ``systemctl restart`` for
systemd, etc.).

V1.5 will replace this manual rotation with OAuth 2.1 client-credentials
flow via the Developer Portal.
"""

from __future__ import annotations

import argparse
import base64
import secrets
import sys


def generate_token() -> str:
    """Return a fresh 32-byte URL-safe base64-encoded token (~43 chars)."""
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_server.scripts.generate_token",
        description=(
            "Generate opaque bearer tokens for the Axiom Oracle MCP server. "
            "Add to AXIOM_MCP_API_KEYS allowlist in AWS Secrets Manager + "
            "restart the ECS service."
        ),
    )
    parser.add_argument(
        "--count", "-n", type=int, default=1,
        help="Number of tokens to generate. Default: 1.",
    )
    args = parser.parse_args(argv)

    if args.count < 1:
        print("--count must be >= 1", file=sys.stderr)
        return 1

    for _ in range(args.count):
        print(generate_token())
    return 0


if __name__ == "__main__":
    sys.exit(main())
