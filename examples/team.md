Areas. An agent started in a folder owns that work:
- api/     : the backend service and its tests.
- webapp/  : the customer-facing front end.
- docs/    : documentation and release notes.
- infra/   : deployment and environments.

Who asks whom:
- Front-end work that needs a new or changed endpoint: ask the api agent, do not edit the backend from webapp/.
- Anything that changes behaviour users see: tell the docs agent when it ships.
- If no agent is in the area you need, start one there with `new`, and write the task so it can work without asking questions: what to produce, where to put it, and who to tell when it is done.

Shared ground:
- All agents share one git checkout. Do not switch branches while others are working; use a worktree.
- Commit only the files you changed.
