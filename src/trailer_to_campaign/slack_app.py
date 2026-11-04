"""The Slack front door (ADR 0002): a Marketer @mentions the app, and one Run starts in its Run Thread.

    door = FrontDoor(films_dir, marketers, runner)
    app = build_app(bot_token, door)          # Bolt, wired to door.handle_mention and door.handle_action

- Only a Marketer can start a Run; anyone else is told so in the thread.
- One Run at a time: a mention while one is going is told to wait.
- The Run Thread is the thread under the mention. A mention inside an old thread starts
  a new Run in a new Run Thread: one Run, one Run Thread.
- Button clicks go to the waiting Run's SlackApprover, which decides who may click. Edit opens
  its form at once (Slack's trigger_id expires within seconds); the saved form goes to that Run.
- close() (the app stopping) ends any wait with ApprovalAborted, so the Run ends in an error.

Slack messages are data: the mention is only read for a Film and a trailer
(slack_request.parse_request), never sent to a model.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trailer_to_campaign.slack_approval import BUTTONS, EDIT, EDIT_FORM, SlackApprover
from trailer_to_campaign.slack_request import Refusal, RunRequest, parse_request
from trailer_to_campaign.slack_thread import SlackThread

log = logging.getLogger(__name__)

Runner = Callable[[RunRequest, SlackApprover, SlackThread], int]


RUN_THREAD = "t2c-run"


def _in_a_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name=RUN_THREAD, daemon=False).start()


def wait_for_runs() -> None:
    """Until a Run mid-stage has ended: the interpreter's shutdown would break its next connection."""
    for thread in threading.enumerate():
        if thread.name == RUN_THREAD:
            thread.join()


class FrontDoor:
    def __init__(
        self,
        films_dir: Path,
        marketers: frozenset[str],
        runner: Runner,
        start: Callable[[Callable[[], None]], None] = _in_a_thread,
    ) -> None:
        self.films_dir, self.marketers, self.runner, self.start = films_dir, marketers, runner, start
        self._busy = threading.Lock()  # one Run at a time
        self._approvers: dict[str, SlackApprover] = {}

    def waiting_run_ids(self) -> list[str]:
        return list(self._approvers)

    def handle_mention(self, event: dict[str, Any], client: Any) -> None:
        channel, ts, user = event["channel"], event["ts"], event.get("user", "")
        reply_ts = event.get("thread_ts") or ts
        _react(client, channel, ts)
        if user not in self.marketers:
            client.chat_postMessage(channel=channel, thread_ts=reply_ts, text="Only a Marketer can start a Run.")
            return
        request = parse_request(event.get("text", ""), self.films_dir)
        if isinstance(request, Refusal):
            client.chat_postMessage(channel=channel, thread_ts=reply_ts, text=request.reason)
            return
        if not self._busy.acquire(blocking=False):
            client.chat_postMessage(
                channel=channel, thread_ts=reply_ts, text="A Run is in progress; try again when it ends."
            )
            return
        run_id = uuid.uuid4().hex[:12]
        try:
            thread_ts = self._run_thread(client, channel, ts, event.get("thread_ts"), request, user)
            approver = SlackApprover(client, channel, thread_ts, self.marketers, run_id)
            self._approvers[run_id] = approver
            self.start(lambda: self._run(request, approver, SlackThread(client, channel, thread_ts), run_id))
        except BaseException:  # the Run never started: free the app for the next one
            self._approvers.pop(run_id, None)
            self._busy.release()
            raise

    def _run_thread(
        self, client: Any, channel: str, ts: str, old_thread: str | None, request: RunRequest, user: str
    ) -> str:
        """Under a top-level mention; for a mention inside an old thread, a new top-level message."""
        if not old_thread:
            return ts
        opened = client.chat_postMessage(channel=channel, text=f"Run for {request.film}, requested by <@{user}>.")
        client.chat_postMessage(
            channel=channel, thread_ts=old_thread, text=f"That starts a new Run, in its own thread: {request.film}."
        )
        return opened["ts"]

    def _run(self, request: RunRequest, approver: SlackApprover, thread: SlackThread, run_id: str) -> None:
        try:
            self.runner(request, approver, thread)
        except Exception as err:  # the Run already ended its own log; say so where the Marketer is looking
            log.exception("the Run for %s stopped", request.film)
            print(f"The Run stopped with an error ({type(err).__name__}). Its Run Log has the details.", file=thread)
        finally:
            # Free the app first: a failed last post must never leave every later request "in progress".
            self._approvers.pop(run_id, None)
            self._busy.release()
            try:
                thread.flush()
            except Exception:
                log.warning("could not post the end of the Run for %s", request.film, exc_info=True)

    def handle_action(self, body: dict[str, Any], client: Any) -> None:
        user, channel = body["user"]["id"], body["channel"]["id"]
        for action in body.get("actions", []):
            value = str(action.get("value", ""))
            approver = self._approvers.get(value.split(":", 1)[0])
            if approver is None:
                client.chat_postEphemeral(
                    channel=channel, user=user, text="That Run is no longer waiting for Approval."
                )
                continue
            if action.get("action_id") == EDIT:  # the form must open now: the trigger_id expires within seconds
                approver.open_editor(client, body.get("trigger_id", ""), value, user)
            else:
                approver.click(action.get("action_id", ""), value, user)

    def handle_submission(self, body: dict[str, Any], client: Any) -> None:
        """A saved edit form goes to the Run waiting on that draft; the approver decides whether it counts."""
        view, user = body.get("view", {}), body.get("user", {}).get("id", "")
        value = str(view.get("private_metadata", ""))
        approver = self._approvers.get(value.split(":", 1)[0])
        if approver is None:
            log.info("an edit was saved for a Run that is no longer waiting: %s", value)
            return
        fields = {
            name: str((inputs.get("value") or {}).get("value") or "")
            for name, inputs in view.get("state", {}).get("values", {}).items()
        }
        approver.submit_edit(value, user, fields)

    def close(self) -> None:
        for approver in list(self._approvers.values()):
            approver.close()


def _react(client: Any, channel: str, ts: str) -> None:
    try:
        client.reactions_add(channel=channel, timestamp=ts, name="eyes")
    except Exception:  # a missing reaction is cosmetic: it must never stop a Run
        log.debug("could not react to %s", ts, exc_info=True)


def build_app(bot_token: str, door: FrontDoor) -> Any:
    """The Bolt app, wired to the door. Bolt acknowledges each event and click within Slack's 3 seconds."""
    from slack_bolt import App

    app = App(token=bot_token)

    @app.event("app_mention")
    def on_mention(event: dict[str, Any], client: Any) -> None:
        door.handle_mention(event, client)

    @app.action(re.compile(f"^({'|'.join(BUTTONS)})$"))
    def on_click(ack: Callable[[], None], body: dict[str, Any], client: Any) -> None:
        ack()
        door.handle_action(body, client)

    @app.view(EDIT_FORM)
    def on_saved_edit(ack: Callable[[], None], body: dict[str, Any], client: Any) -> None:
        ack()  # closes the form; what the edit means is the Run's to decide
        door.handle_submission(body, client)

    return app
