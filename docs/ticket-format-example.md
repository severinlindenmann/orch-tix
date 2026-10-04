# Ticket format example

Generated with `orch schema example` (orch-core schema 1.4.0). Do not edit by hand.

This is the document the orch-tix addon builds a mirror from (`ctx.document`). What reaches the phone is a redacted view of it; see the `redaction` setting in `addons/orch-tix/README.md`.

```json
{
  "schema_version": "1.4.0",
  "id": "DEMO-0038",
  "title": "Export the meter readings as CSV",
  "type": "feature",
  "priority": "high",
  "size": "m",
  "status": "waiting",
  "created": "2026-10-02T09:00Z",
  "updated": "2026-10-02T09:00Z",
  "labels": [
    "export"
  ],
  "parent": null,
  "blocked_by": [],
  "follow_ups": [],
  "external": [],
  "repos": [],
  "branches": {},
  "prs": [],
  "sprint": null,
  "claim": {
    "session": "7f3c9a21",
    "harness": "claude-code",
    "at": "2026-10-02T09:00Z"
  },
  "gates": {
    "requirements": {
      "state": "approved",
      "hash": "sha256:eed4a231276678a366a47ea903846470742c5251b2323a34c5b0f0bf23459daa",
      "covers": [
        "Requirements",
        "Acceptance criteria",
        "Out of scope",
        "size",
        "type"
      ],
      "approved": "2026-10-02T09:00Z",
      "via": "dashboard",
      "changes_requested": null
    },
    "plan": {
      "state": "approved",
      "hash": "sha256:4ef7c8bc5c13ded5a90c946bff7b6b0950f9db15f3089cbe117cfe68182fa59e",
      "covers": [
        "Plan"
      ],
      "approved": "2026-10-02T09:00Z",
      "via": "dashboard",
      "changes_requested": null
    },
    "verify": {
      "verdict": null,
      "at": null,
      "via": null
    }
  },
  "questions": [
    {
      "id": "Q1",
      "text": "Which timestamp format?",
      "why": "Excel parses only some formats",
      "type": "single",
      "options": [
        {
          "key": "A",
          "label": "ISO 8601",
          "cost": "none"
        },
        {
          "key": "B",
          "label": "Local time",
          "cost": null
        }
      ],
      "recommended": "A",
      "blocking": true,
      "asked": "2026-10-02T09:00Z",
      "answer": null,
      "note": null,
      "answered": null,
      "via": null,
      "hash": "sha256:d001617f80066f8d8fa4baaec5a10e45f14f9cbab0cc32344e4d2e0c9648aa7e"
    }
  ],
  "sections": {
    "Ask": "The client needs the readings as CSV.",
    "Summary": "",
    "Context": "",
    "Requirements": "- One file per day",
    "Acceptance criteria": "- [ ] Opens in Excel",
    "Out of scope": "",
    "Plan": "1. Inventory\n2. Exporter",
    "Tasks": "- [x] T1 Inventory the meter export jobs\n  - ref: ac:1\n  - note: 14 jobs listed\n- [/] T2 Write the CSV exporter\n  - needs: T1\n  - verify: pytest tests/test_export.py\n- [ ] T3 Ask the client for the delimiter\n  - owner: human",
    "Current state": "",
    "Verification": "",
    "Log": "",
    "Findings": ""
  },
  "tasks": {
    "format": "orch.tasks.v1",
    "ticket": "DEMO-0038",
    "status": "waiting",
    "error": null,
    "summary": {
      "total": 3,
      "todo": 1,
      "doing": 1,
      "done": 1,
      "skipped": 0,
      "blocked": 0,
      "closed": 1
    },
    "doing": "T2",
    "next": "T2",
    "open": [
      "T2",
      "T3"
    ],
    "can_move_to_testing": false,
    "plan_approved": "2026-10-02T09:00Z",
    "added_since_approval": 0,
    "tasks": [
      {
        "id": "T1",
        "state": "done",
        "text": "Inventory the meter export jobs",
        "owner": "agent",
        "needs": [],
        "needs_open": [],
        "refs": [
          {
            "kind": "ac",
            "target": "1",
            "label": null,
            "exists": true,
            "text": "Opens in Excel"
          }
        ],
        "verify": null,
        "why": null,
        "on": null,
        "on_ref": null,
        "note": "14 jobs listed",
        "added": null,
        "added_after_approval": false,
        "waits_on_you": false
      },
      {
        "id": "T2",
        "state": "doing",
        "text": "Write the CSV exporter",
        "owner": "agent",
        "needs": [
          "T1"
        ],
        "needs_open": [],
        "refs": [],
        "verify": "pytest tests/test_export.py",
        "why": null,
        "on": null,
        "on_ref": null,
        "note": null,
        "added": null,
        "added_after_approval": false,
        "waits_on_you": false
      },
      {
        "id": "T3",
        "state": "todo",
        "text": "Ask the client for the delimiter",
        "owner": "human",
        "needs": [],
        "needs_open": [],
        "refs": [],
        "verify": null,
        "why": null,
        "on": null,
        "on_ref": null,
        "note": null,
        "added": null,
        "added_after_approval": false,
        "waits_on_you": false
      }
    ]
  },
  "needs": [
    {
      "kind": "answer",
      "detail": "Q1"
    },
    {
      "kind": "task",
      "detail": "T3"
    }
  ],
  "artifacts": [],
  "verdict": null,
  "move": {
    "who": "you",
    "kind": "answer",
    "label": "Answer Q1",
    "ref": "Q1",
    "why": "The agent asked a blocking question and waits for your answer."
  }
}
```
