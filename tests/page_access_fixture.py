"""Assume granted page access in pre-existing, unrelated queue regressions."""
from unittest.mock import patch


def granted_access(test):
    for p in (patch("page_access.members", return_value={uid: {"page"} for uid in (11, 22, 33)}),
              patch("page_access.can_receive", return_value=True), patch("client_workflow.queue_board")):
        p.start()
        test.addCleanup(p.stop)
