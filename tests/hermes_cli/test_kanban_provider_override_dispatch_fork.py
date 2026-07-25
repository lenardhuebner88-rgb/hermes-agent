"""Fork-side coverage: a per-task ``provider_override`` must reach the worker.

Why this file exists
--------------------
Upstream #69876 added the ``provider_override`` column plus a dashboard
dropdown that writes ``model_override`` and ``provider_override`` as two
separate values. Upstream's dispatcher reads the column directly when it
builds the worker argv.

The fork does not build argv from the mutable task row. It stamps an
**immutable spawn identity** at claim time (``_spawn_identity_metadata`` ->
``_persisted_spawn_identity``, both fork-only symbols) so that a lane edit
between claim and launch cannot change the argv of a run already in flight.
``_default_spawn`` then honours that stamp: ``route_is_frozen`` is true for
every really-claimed task.

The consequence, measured before this file was written: the stamp resolved
only ``model_override`` and never looked at ``provider_override``, so
upstream's branch

    elif task.provider_override and not route_is_frozen:

was unreachable in production. A task carrying ``grok-build-0.1`` +
``xai-oauth`` was silently dispatched on the *profile's* model and provider —
the model override was dropped too, because it was validated against the
profile's provider and judged a poison pill.

So the fix belongs in the stamp, not in the argv builder: the stamp is the
fork's single source of truth for a run's route. These tests pin that
contract. The equivalence test is the important one — it says the two ways to
express "this model on that provider" must resolve identically, which is what
keeps the fork's pair syntax and upstream's two-column form from drifting.

Inputs come from the static provider map in ``hermes_cli.models`` (a code
table, not user config), so they are deterministic:

* ``anthropic`` + ``claude-opus-4-8``   -> compatible
* ``openai-codex`` + ``claude-opus-4-8`` -> poison pill (belongs to anthropic)
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb

# Deterministic pair from the static map: detect_static_provider_for_model
# returns None for this combination (model is in the provider's family).
GOOD_PROVIDER = "anthropic"
GOOD_MODEL = "claude-opus-4-8"
# The same model against an unrelated provider: the static map attributes it
# to "anthropic", so the fork must refuse it rather than mis-route the run.
WRONG_PROVIDER = "openai-codex"
# A model the static map knows nothing about. detect_static_provider_for_model
# returns None for it against ANY provider, so the family check raises no
# objection — the only thing that can stop a bogus provider here is validating
# the provider itself.
UNKNOWN_MODEL = "some-unknown-model-xyz"


def _stamp(conn, tid):
    """The spawn identity as the dispatcher would resolve it for this task."""
    task = kb.get_task(conn, tid)
    assert task is not None
    return kb._spawn_identity_metadata(
        task.assignee,
        model_override=task.model_override,
        provider_override=task.provider_override,
        task_id=tid,
        conn=conn,
    )


def test_stamp_honours_two_column_provider_override(kanban_home):
    """The bare column form must pin both model and provider in the stamp."""
    conn = kb.connect()
    with conn:
        tid = kb.create_task(
            conn,
            title="two-column override",
            assignee="default",
            model_override=GOOD_MODEL,
            provider_override=GOOD_PROVIDER,
        )

    identity = _stamp(conn, tid)

    assert identity is not None
    assert identity["model"] == GOOD_MODEL
    assert identity["route_provider"] == GOOD_PROVIDER
    assert identity["model_source"] == "task_override_with_provider_switch"


def test_two_column_form_matches_pair_syntax(kanban_home):
    """``provider_override`` + model == the fork's ``provider/model`` string.

    This is the invariant that keeps the two spellings from drifting. If it
    ever fails, one of the two paths has grown a special case.
    """
    conn = kb.connect()
    with conn:
        two_col = kb.create_task(
            conn,
            title="two-column",
            assignee="default",
            model_override=GOOD_MODEL,
            provider_override=GOOD_PROVIDER,
        )
        pair = kb.create_task(
            conn,
            title="pair syntax",
            assignee="default",
            model_override=f"{GOOD_PROVIDER}/{GOOD_MODEL}",
        )

    a, b = _stamp(conn, two_col), _stamp(conn, pair)
    for key in ("model", "route_provider", "model_source", "provider"):
        assert a[key] == b[key], f"{key} differs: two-column={a[key]!r} pair={b[key]!r}"


def test_provider_override_still_refuses_a_poison_pill(kanban_home):
    """An explicit provider must not buy a bypass of the family check.

    Control probe for the test above: if the fix simply trusted whatever
    provider the caller sent, this test would go green with a mis-routed run.
    """
    conn = kb.connect()
    with conn:
        tid = kb.create_task(
            conn,
            title="poison pill",
            assignee="default",
            model_override=GOOD_MODEL,
            provider_override=WRONG_PROVIDER,
        )

    identity = _stamp(conn, tid)

    # A refused override yields no route from the override itself: the stamp
    # carries no "model" key and model_source stays "unknown". Here that is the
    # whole stamp because this fixture's profile has no configured model either,
    # so the dispatcher goes on to fail closed with "no concrete model route".
    # With a profile (or lane) model configured, the refusal instead falls
    # through to that model — the point being that the poison-pill pair never
    # reaches the worker, not that every refusal aborts the spawn.
    assert identity is not None
    assert identity.get("model") is None
    assert identity.get("route_provider") is None
    assert identity["model_source"] == "unknown"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "model_override_incompatible" in kinds


def test_provider_override_is_canonicalized_like_the_pair_form(kanban_home):
    """A provider alias must resolve to its canonical id in the stamp.

    ``kimi`` is an alias for ``kimi-coding``. The pair form canonicalizes via
    ``parse_model_input``; if the two-column form did not, the alias would reach
    both the worker argv and the persisted cost stamp, and the run would be
    billed against a provider name nothing else in Hermes uses.
    """
    conn = kb.connect()
    with conn:
        aliased = kb.create_task(
            conn,
            title="aliased provider",
            assignee="default",
            model_override="kimi-k2.7-code",
            provider_override="kimi",
        )
        pair = kb.create_task(
            conn,
            title="aliased pair",
            assignee="default",
            model_override="kimi/kimi-k2.7-code",
        )

    a, b = _stamp(conn, aliased), _stamp(conn, pair)
    assert a["route_provider"] == "kimi-coding"
    assert a["route_provider"] == b["route_provider"]


def test_unresolvable_provider_override_is_refused(kanban_home):
    """An unknown provider must not be forwarded as ``--provider``.

    Unlike a slash inside a model id, this column is unambiguously a provider,
    so a value the model registry cannot resolve is an operator error. Fail
    closed rather than hand the worker a backend name that does not exist.

    The model here must be UNKNOWN to the static provider map. With a known
    model the family check refuses the pair on its own, so the test would pass
    whether or not the provider itself was ever validated — it has to be the
    case where nothing else objects.
    """
    conn = kb.connect()
    with conn:
        tid = kb.create_task(
            conn,
            title="bogus provider",
            assignee="default",
            model_override=UNKNOWN_MODEL,
            provider_override="totally-bogus",
        )

    identity = _stamp(conn, tid)

    assert identity is not None
    assert identity.get("route_provider") != "totally-bogus"
    assert identity["model_source"] != "task_override_with_provider_switch"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "model_override_incompatible" in kinds


def test_reviewer_run_does_not_inherit_the_task_provider(kanban_home):
    """A coder task's provider must not retarget a different reviewer profile.

    ``claim_review_task`` resolves the review-gate's ``verifier_profile``, which
    can differ from the task's assignee. Silently moving that review run onto the
    task's provider would bill review work against a backend the operator never
    chose for it.
    """
    conn = kb.connect()
    with conn:
        tid = kb.create_task(
            conn,
            title="own run applies",
            assignee="default",
            model_override=GOOD_MODEL,
            provider_override=GOOD_PROVIDER,
        )

    # A real second profile on disk WITH its own configured route. Without the
    # config the reviewer stamp comes back None and the assertion would pass for
    # the wrong reason (no stamp at all, rather than a stamp that kept its own
    # provider). With it, the reviewer must resolve to openai-codex.
    verifier_home = kanban_home / "profiles" / "verifier"
    verifier_home.mkdir(parents=True, exist_ok=True)
    (verifier_home / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n  name: gpt-5.6-sol\n", encoding="utf-8"
    )

    own = kb._spawn_identity_metadata(
        "default", model_override=GOOD_MODEL, task_id=tid, conn=conn
    )
    reviewer = kb._spawn_identity_metadata(
        "verifier", model_override=GOOD_MODEL, task_id=tid, conn=conn
    )

    assert own is not None and reviewer is not None
    assert own["route_provider"] == GOOD_PROVIDER
    assert reviewer["route_provider"] == "openai-codex"
    assert reviewer["model_source"] == "profile"


def test_worker_argv_carries_the_two_column_provider(
    kanban_home, all_assignees_spawnable, monkeypatch
):
    """End of the chain: the real argv the worker process would receive.

    Runs the real dispatcher and the real ``_default_spawn``; only the OS
    process launch is intercepted, so the whole provider/lane precedence
    branch executes for real.
    """
    captured: list = []
    monkeypatch.setattr(
        kb, "_launch_worker_process", lambda spec: (captured.append(spec), 4242)[1]
    )

    conn = kb.connect()
    with conn:
        tid = kb.create_task(
            conn,
            title="argv probe",
            assignee="default",
            model_override=GOOD_MODEL,
            provider_override=GOOD_PROVIDER,
        )

    kb.dispatch_once(conn, serialize_by_repo=False)

    specs = [s for s in captured if s.env.get("HERMES_KANBAN_TASK") == tid]
    assert specs, f"task {tid} was never dispatched (captured {len(captured)} spawns)"
    argv = list(specs[0].argv)

    assert "--provider" in argv, f"no --provider in argv: {argv}"
    assert argv[argv.index("--provider") + 1] == GOOD_PROVIDER
    assert "-m" in argv, f"no -m in argv: {argv}"
    assert argv[argv.index("-m") + 1] == GOOD_MODEL
    # `-m` is a chat-subparser flag: before `chat` the subparser default
    # clobbers it and the override never reaches the worker.
    assert argv.index("-m") > argv.index("chat")
