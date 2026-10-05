"""Copy the temporary native token and queue one Modal provisioning call."""

import os
import re
import stat
import sys
from uuid import UUID

import modal


def read_order(environ):
    pool = environ["CLAUDE_RUNNER_POOL_ID"]
    order_id = environ["CLAUDE_RUNNER_ORDER_ID"]
    session = environ["CLAUDE_RUNNER_SESSION_UUID"]
    if (
        pool != environ["CLAUDE_ENVIRONMENT_ID"]
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", order_id)
        or str(UUID(session)) != session
    ):
        raise ValueError("Invalid work order")
    # The native runner deletes this file when the hook returns. Copy the
    # opaque bytes before spawn(); do not parse or log the token.
    descriptor = os.open(environ["CLAUDE_RUNNER_WORK_ORDER_FILE"], os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Work order must be a regular file")
        token = source.read(1024 * 1024 + 1)
    if not token.strip() or len(token) > 1024 * 1024:
        raise ValueError("Invalid work order size")
    return dict(environment_id=pool, order_id=order_id, session_uuid=session, token=token)


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError("Unexpected hook arguments")
        order = read_order(os.environ)
        app_name = os.environ["APP_NAME"]
    except (KeyError, ValueError):
        return 2  # Invalid configuration/order: requires operator attention.
    except OSError:
        return 1  # Temporary token read failure.
    try:
        modal.Function.from_name(
            app_name, "provision", environment_name=os.environ.get("MODAL_ENVIRONMENT")
        ).spawn(order)
    except Exception:
        # A lost acknowledgement may still have queued the call. The
        # provisioner's immutable claims make redelivery safe.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
