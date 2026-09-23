# ADR 0016 — The event codec enforces the schemas' numeric bounds, and the aggregator encodes a transition before persisting it

Status: accepted 2026-09-23 (issue #112). It amends two earlier ADRs. Each
amended place carries a dated note pointing here, and each amended ADR gains
a short amendment section listing its notes:

* **ADR-0015 (Amendment 4).** Amendment 3 ruling 3 said "Schema minimums and
  maximums are not added" and listed the schemas' bounds as found and not
  ruled. Decisions 1 and 2 below add them.
* **ADR-0011 (Amendment 8).** Decision 4's step order changes as decision 3
  below says. Decision 3 step 1 gains a note on what `MALFORMED` now covers.

No schema file changes.

Scope note: this ADR settles what issue #112's fix is implemented against.
It covers two modules:

* `hammertime.core.events.codec`: what it refuses, on decode and on encode,
  in the payloads' integer fields;
* `services/aggregator/src/hammertime/aggregator/transitions.py`: the order
  of `TransitionEmitter.evaluate`'s encode and persist steps.

`AggregatorWorker` is not changed: `_decode` already turns every
`CodecError` into `MALFORMED` (ADR-0011 decision 3 step 1). Assumption 11
lists what is left open: the codec's string fields, array lengths other than
`maxItems`, the size of its error messages, and cross-field consistency.

## Context

### The defect

1. **A negative `request_count` stops the aggregator, and the redelivery
   stops it again.** `codec.decode` checks that each observation's
   `request_count` is a JSON integer (ADR-0015 Amendment 3 ruling 3). It does
   not check that the value lies within `schemas/observation.v1.json`'s
   bounds, 0 to 1,000,000,000. `AggregatorWorker._handle` passes the decoded
   value unchecked to `ShardWindow.observe`. `IpCounter.observe` raises
   `ValueError` for a negative delta, as ADR-0011 decision 2 says it must
   ("`delta < 0` is a `ValueError`"). Nothing catches the error:
   * not `_handle`, `handle()` or `run()`;
   * not `AggregatorService.run()`, which collects the consume task's
     exception and exits 1 with `run_exited` (ADR-0013 decision 8, as amended
     by Amendment 4 ruling R7).

   The message is never marked handled, so it is never acknowledged. The
   aggregator's durables have `max_deliver = -1` (ADR-0013 decision 5;
   `packages/hammertime-bus/src/hammertime/bus/nats.py`), so the message is
   delivered again to every restart. The crash loop lasts until someone
   purges the message by hand.
2. **Same root cause, second trigger.** Take an IP that is COLD. It receives
   `request_count` 1, then `request_count` `10**4300 - 1`. That value has
   4,300 digits, which `json.loads` still parses under CPython's default
   integer-string limit (ADR-0015 assumption 61). The window total becomes
   `10**4300`, which has 4,301 digits. Two things then happen:
   * the COLD -> HOT edge is recorded HOT in the state store (ADR-0011
     decision 4 step 2);
   * `encode` of the `HotIpAdded` raises `CodecError`, because `json.dumps`
     cannot print the count.

   That error also escapes `handle()`. The store now says HOT and the log
   holds nothing. (A single `request_count` of 4,301 digits or more was
   already refused, because `json.loads` cannot parse it; it takes two
   messages to get past the limit.)

Both triggers break ADR-0011 decision 3 step 1: "Any failure, including
`CodecError`, is `MALFORMED` … A poison message never stops the consumer."
Both also break the premise ADR-0013 decision 5 gives for `max_deliver = -1`:
"every message the aggregator receives is acknowledged on one of decision
8's paths … so a poison message cannot loop".

### Who can send such a message

Only a principal that can publish directly to `hammertime.observations.v1`.
Ingest refuses both values at the edge, three times over:

* `ObservationSchemaValidator` validates the request body against
  `schemas/observation.v1.json`;
* the Pydantic model declares `request_count: int = Field(ge=0,
  le=1_000_000_000)`;
* `ObservationPublisher.coalesce` refuses a coalesced total above the
  maximum.

The bus, however, is inside the trust boundary. In the reference deployment
anything that can connect to port 4222 can publish (ADR-0013 assumption 13,
as amended by Amendment 4 ruling S1). The spec asks for protection in any
case. §36 says "The service SHOULD also protect against malicious agents
sending extremely large numbers". §30's processing algorithm begins
`on_observation(message): validate(message)`. Ingest provides that
protection at the edge. This ADR applies the same bounds at the consumer,
because a principal on the bus need not go through ingest.

### What earlier ADRs fixed, and what they left open

* ADR-0015 Amendment 3 ruling 3 made each of the codec's integer fields
  accept exactly a JSON integer. Its assumption 64 put that fix in the codec
  rather than in the aggregator's `except`: "Keeping that contract in the
  codec fixes every consumer at once. Widening the aggregator's `except`
  would fix one, and a catch-all there would also turn a codec bug into a
  silently dropped message." The same ruling said "Schema minimums and
  maximums are not added". Its "Found while ruling, not ruled here" list
  named them: "The schemas' `minimum` and `maximum` are not enforced by the
  codec. Examples: `sequence >= 0`, `window_seconds` 1-3600, `request_count
  <= 10**9`."
* ADR-0015 assumption 66: "The encoder refuses what the decoder would
  refuse".
* ADR-0011 decision 4 emits a transition in five steps: take the sequence;
  `record_transition`; build the payload; build the envelope and publish it;
  `set_state`. Encoding happens inside the publish step, after the durable
  write.
* ADR-0011 assumption 10: a `MALFORMED` message is dropped, not diverted.
* `hammertime.core.events.models`: "the JSON Schema files are
  authoritative".

## Decision

### 1. `decode` refuses a payload integer outside its schema's `minimum` and `maximum`

This covers every payload field the codec converts to `int`: the eight in
ADR-0015 Amendment 3 ruling 3's list. After the JSON-integer check, each is
checked against the inclusive `minimum` and `maximum` its schema states. A
value outside them is a `CodecError`. The bounds are exactly the schemas';
none is added and none is left out:

| Schema | Field | `minimum` | `maximum` |
| --- | --- | --- | --- |
| `observation.v1.json` | `sequence` | 0 | 9223372036854775807 (2\*\*63 − 1) |
| `observation.v1.json` | `window_seconds` | 1 | 3600 |
| `observation.v1.json` | `observations[].request_count` | 0 | 1000000000 |
| `hot_ip_event.v1.json` (`HotIpAdded` and `HotIpRemoved`) | `sequence` | 0 | none |
| `hot_ip_event.v1.json` (`HotIpAdded` and `HotIpRemoved`) | `window_count` | 0 | none |
| `hot_ip_event.v1.json` (`HotIpAdded` and `HotIpRemoved`) | `config_version` | 1 | none |
| `prefix_stats_event.v1.json` | `hot_count` | 0 | none |
| `prefix_stats_event.v1.json` | `sequence` | 0 | none |

* **Inclusive** has JSON Schema's meaning. An instance satisfies `minimum`
  if it is "greater than or exactly equal to" it, and `maximum` if it is
  "less than or equal to" it (Validation 2020-12 §6.2.4 and §6.2.2;
  Sources). So 0 and 1000000000 are valid `request_count` values; −1 and
  1000000001 are not.
* **"none"** means the schema states no maximum, and the codec adds none.
  Such a field accepts any JSON integer at or above its minimum that
  `json.loads` can parse.
* **The type check comes first.** `true` is still refused as a type error.
  An integral float such as `-1.0` is first converted to `-1`, then refused
  by the bound. Both are a `CodecError`.
* **Not changed:**
  * the envelope's `schema_version` (still pinned to 1), `sequence` and
    `config_version` (assumption 3);
  * `capacity`, a decimal string, whose `[0-9]+` rule (ADR-0015 assumption
    65) already keeps it `>= 0`;
  * properties the codec does not read, such as `hot_ip_event.v1.json`'s
    `shard` and `prefix_stats_event.v1.json`'s `hot_ratio`. They stay
    unchecked because they are ignored, as ADR-0015 assumption 64 rules;
  * `observations`' `maxItems`, which is already enforced, and its
    `minItems`, which is not (assumption 9).
* **Messages.** The range check's own text names the field and the bound
  it broke, never the value, as the JSON-integer check's text already does.
  The wrappers around both checks (for example `malformed observation entry:
  …`) are unchanged; bounding them remains ADR-0015 Amendment 3's open item.
  The wording is not part of the contract.
* **No in-repo producer is affected.** Every in-repo producer already
  writes values in range:
  * ingest validates at the edge (Context);
  * the aggregator's emitter writes a window total, which is never negative;
    a shard sequence, which is never negative either, because it starts
    from the store's `next_sequence` (0 for a new shard) and only grows
    (ADR-0011 decision 5, A2); and `DetectionConfig.config_version`, which
    is `>= 1`.

### 2. `encode` refuses the same values

ADR-0015 assumption 66 says the encoder refuses what the decoder would
refuse. That now applies to the bounds too. Each of the eight payload fields
(the three hot-ip fields on both `HotIpAdded` and `HotIpRemoved`) must hold
an `int` that is not a `bool` and that lies within the table's bounds;
otherwise `encode` raises `CodecError`. The envelope is unchanged
(assumption 3).

One more property is now part of the encode contract, for every integer
field. An integer whose decimal text the interpreter refuses to produce is a
`CodecError` on encode. Under CPython's default limit that means more than
4,300 digits. This was already so in practice, because `encode` turns
`json.dumps`'s `ValueError` into `CodecError`. ADR-0015 assumption 66 stated
it for `capacity` only.

### 3. `TransitionEmitter.evaluate` encodes the event before it writes the durable HOT set

When an edge is to be emitted, ADR-0011 decision 4's steps become:

1. `sequence = window.next_sequence`; `window.next_sequence += 1`.
   Unchanged.
2. Build the payload and the envelope, exactly as decision 4's steps 3 and
   4 describe (`timestamp = datetime.fromtimestamp(clock.now(), tz=UTC)`),
   and `encode` the envelope. A `CodecError` propagates. Nothing has been
   persisted or published, the IP's state in the window is unchanged, and
   no transition is counted. The sequence number is consumed, as it already
   is for a store failure (assumption 7).
3. `await state_store.record_transition(window.shard, ip, new, sequence)`.
   Persist before publish, unchanged. A failure propagates exactly as
   before: nothing is published, the state in memory is unchanged, and the
   sequence number is consumed.
4. Publish the bytes encoded in step 2 to `hammertime.hot-ip.v1` under
   `key_selector(payload)`, with `message_id = envelope.event_id` (ADR-0013
   decision 4), and await it. Unchanged apart from reusing the bytes.
5. `window.set_state(ip, new)`; count the transition under `reason`; return
   the `EmittedTransition`. Unchanged.

Only the build-and-encode moves. It is pure computation, and it was the one
step between the durable write and the publish that could fail without any
I/O. Decision 4's "persist before publish" and its reason stand: the trie
still cannot learn of an IP that no owner holds as HOT.

What the move removes is the one way a deterministic failure could leave the
store ahead of the log. "Deterministic" here means a failure that is not a
crash or an outage, and so recurs every time the same input is processed.
After decision 1 no observation can make `encode` fail on this path
(decision 4). The move therefore defends against bugs: whatever makes
`encode` fail, it cannot leave the store and the log diverged.

### 4. The aggregator gains no `except`

`AggregatorWorker` is not changed. `_decode` still catches only `CodecError`
and returns `MALFORMED` for it, as ADR-0011 decision 3 step 1 says. With
decision 1 in the codec, an observation message whose `request_count`,
`sequence` or `window_seconds` is out of range is `MALFORMED`:

* logged as `malformed_observation`;
* counted in `observations_rejected{reason="malformed"}`;
* not diverted (ADR-0011 assumption 10) and not applied;
* marked handled, and therefore acknowledged at the next commit.

The consumer carries on. This is how the fix fits ADR-0015 assumption 64:
the contract lives in the codec, which every consumer shares, and no
consumer catches more than `CodecError`.

What follows for the window, the emitter and the other outcomes:

* **The counter's check stays, and cannot be reached from the bus.**
  `IpCounter.observe`'s `ValueError` for a negative delta remains the
  counter's own argument check (ADR-0011 decision 2). Every delta the worker
  passes it is a decoded `request_count`, and that is now between 0 and
  1,000,000,000.
* **The second trigger cannot happen.** A window count is a sum of applied
  deltas, each at most `10**9`. To pass the integer-string limit of about
  4,300 digits it would need more than `10**4290` applied messages in one
  window. So no observation can make the emitter's `encode` fail, and the
  only encode failure left on that path is a bug. Decision 3 keeps a bug
  from leaving the store and the log diverged.
* **`window_seconds`.** `WINDOW_TOO_LONG` (ADR-0010 decision 6; ADR-0011
  decision 3 step 2) still covers every `window_seconds` above
  `config.window_seconds` up to 3600. A `window_seconds` below 1 or above
  3600 is now `MALFORMED` (assumption 5): it is dropped and counted under
  `reason="malformed"`. Before, one above the configured window was
  diverted to reconciliation and counted under `reason="window_too_long"`,
  and any other out-of-range value was applied.
* **`sequence`.** A payload `sequence` below 0 or above 2\*\*63 − 1 is
  `MALFORMED`. It used to be applied; the aggregator never reads it.
* **An encode failure in the emitter is not caught either.** After decision
  1 only a bug can cause one. It ends the consume task and the service exits
  1, as a state-store failure does (ADR-0011 assumption 7). By decision 3,
  nothing was persisted first.
* **A deployment already in the crash loop recovers on upgrade**, with no
  purge. The redelivered message is `MALFORMED` and is acknowledged.

### 5. What is not changed

* No schema file. The bounds enforced are the ones the schemas already
  state.
* ADR-0013 decision 5's `max_deliver = -1` (assumption 8).
* ADR-0011 decision 3's outcomes and their order, and decision 8's
  counters, labels and log records.
* Ingest, the bus and the store.

## Assumptions

Each is a judgment call that issue #112, the spec and the earlier ADRs do
not make. Push back on them individually.

1. **Bounds in the codec only.** Issue #112 offered three places: the codec,
   the aggregator's `_decode`, or both. I chose the codec alone, for ADR-0015
   assumption 64's reason: a contract kept in the codec fixes every consumer
   at once.

   A second check in the aggregator would restate the schema bounds in a
   second place. It would also guard no path, because the worker has none
   into `_handle` that bypasses `decode`. ADR-0015 decision 5 is different:
   the owner ruled defence in depth for the attribute record map because that
   map is written without the codec (a snapshot loader, an in-process
   producer, a test harness). The aggregator's window has no such writer.
2. **All eight payload fields, not only `request_count`.**
   * `request_count` is the only one that stops the aggregator. But a
     `window_seconds` below 1 and a `sequence` out of range were applied
     without complaint.
   * The hot-ip and prefix-stats fields have no running consumer today
     (ADR-0015 assumption 69). The trie and the detector will consume them.
     One rule now is cheaper than one ruling per consumer later.
   * The schemas are authoritative (`hammertime.core.events.models`).

   The alternative, `request_count` alone, was rejected. It would still
   admit three known schema violations and would need another ADR later.
3. **No bounds on the envelope's `sequence` and `config_version`.** No schema
   file describes the envelope. The only rule its integer fields have is
   ADR-0015 assumption 64's type rule, applied to them by analogy.

   I considered the derived bounds `sequence >= 0` and `config_version >= 1`.
   They mirror the payload fields these fields duplicate, and
   `DetectionConfig`. I did not adopt them, for two reasons:
   * No consumer reads these fields except to derive `event_id`, and
     `compute_event_id` only formats the value into text. An out-of-range
     value cannot make anything fail.
   * Bounding a copy without checking that it equals the payload's original
     would be half a rule.

   Push back if uniformity is wanted. It would be two more rows, lower
   bounds only, and no in-repo producer writes a value they would refuse.
   The tests pin the current choice, so it cannot change unnoticed.
4. **The encoder is bounded too** (ADR-0015 assumption 66, extended).
   * *Benefit.* A producer bug becomes a loud `CodecError` at the producer,
     not a message that every consumer drops. Ingest turns any publish
     exception into a 503 that it logs (`api/routes.py`).
   * *Cost.* `encode` no longer produces an out-of-range message, so a test
     that needs one must build the bytes itself by editing encoded JSON.
     `test_codec.py` already does this for the type rule.
5. **A `window_seconds` above 3600 is `MALFORMED`, not `WINDOW_TOO_LONG`.**
   The alternative was to leave `window_seconds`'s maximum out of decision
   1, so that the aggregator could still divert such a message to
   reconciliation. I rejected it for three reasons:
   * A schema-invalid message is malformed. ADR-0011 assumption 10's reason
     for dropping rather than diverting ("cannot be trusted") applies to it.
   * No agent can send one: ingest answers 400.
   * An exception to "the schema's bounds" would need its own justification
     in every later reading.
6. **The emitter's step order changes (decision 3).** Four answers to the
   issue's question (saturate, reject, or handle the failure) were weighed:
   * *Cap or saturate the window count.* Rejected. §5's running total is
     exact, and a cap would hide an invalid input instead of refusing it.
     `hot_ip_event.v1.json` gives `window_count` no maximum, so no wire rule
     calls for one. After decision 1 the count cannot get near the limit
     anyway.
   * *Catch the `CodecError` in the emitter or the worker, log it and skip
     the transition.* Rejected. The IP would stay COLD in memory while over
     threshold, and every later evaluation would retry and fail again. That
     is a detection failure that shows up only in a log. It is ADR-0015
     assumption 64's reason against a catch-all, applied to a transition
     instead of a message.
   * *Leave the order as it is.* Defensible. After decision 1 the failure
     can only come from a bug, and "store ahead of log" is the divergence
     ADR-0011 decision 4 ("Why persist before publish") already accepts
     after a crash. Not chosen, because the fix below costs one code move
     and nothing at run time.
   * *Encode before persisting.* Chosen. It leaves no deterministic failure
     between persist and publish.

   One side effect: the envelope's `timestamp` is taken just before the
   store write rather than just after it. Nothing requires one instant over
   the other, and a `ManualClock` test cannot tell them apart.
7. **An encode failure consumes the sequence number.** The number is taken
   in step 1, before the envelope that needs it is built. Consuming it keeps
   one rule for every failure after step 1, the same as for a store failure.
   The window's `next_sequence` then runs one ahead of the store's, which
   only leaves a gap. After a restart the new owner loads the store's lower
   value and may reuse that number. That is safe, because nothing was
   published under it.
8. **`max_deliver` stays unlimited.** A bounded `max_deliver` would turn any
   future content-triggered crash loop into a silent drop after N
   deliveries. That is the catch-all trade ADR-0015 assumption 64 rejects.
   With this ADR every message is again acknowledged on one of ADR-0013
   decision 8's paths, so decision 5's premise holds again. A dead-letter
   design would be a separate decision; this issue does not need one.
9. **The codec does not enforce `observations`' `minItems: 1`.** It is an
   array length, not an integer bound. The aggregator's own one-entry check
   already makes an empty array `MALFORMED`, and nothing raises on one. The
   codec's existing `maxItems` check is unchanged.
10. **The codec keeps the bounds as constants, and the tests read the schema
    files.** The codec already mirrors a schema value as a constant for
    `maxItems` (`_MAX_OBSERVATIONS`, citing its schema). Reading `schemas/`
    at import would make a core wire rule depend on a file outside every
    package; ingest's validator has to walk up the directory tree to find it
    (`services/ingest/src/hammertime/ingest/validation/schema.py`). The
    tests instead read each bound from the schema file, found the same way,
    so a schema edit the codec does not follow fails the suite.
11. **Left open.** None of these can make a consumer raise:
    * string lengths (`agent_id` 1-128) and the codec's `str()` coercion of
      string fields (ADR-0015 Amendment 3's second open item);
    * the size of wrapper error messages that embed a whole entry or
      payload (its first);
    * cross-field consistency: the envelope `sequence` against the payload
      `sequence`, `hot_count <= capacity`, and `capacity >= 1`. The schemas
      do not state these, and the detector epic should not assume them.
12. **One `CHANGES` entry, not `BREAKING`.**
    * *Why an entry.* The running aggregator changes what it does with an
      out-of-range observation message. Before, it stopped, applied the
      message, or diverted it. Now it counts the message as malformed and
      skips it. That is visible in the exit status, a metric and the log.
      It is `CLAUDE.md`'s "changed behaviour", with ADR-0015 assumption 69
      as the precedent for the same kind of change.
    * *What gets no entry.* The hot-ip and prefix-stats bounds have no
      running consumer. The encoder's bounds and the emitter's new order
      change nothing any producer in this repository does. None of these
      changes anything a deployment observes (ADR-0015 assumption 27's
      reasoning, as assumption 69 applies it).
    * *Why not `BREAKING`.* No deployment has to act. Every producer here
      already writes in-range values, and the schemas already required them.

    The line, recorded here so the implementing change does not have to
    re-derive it:

        Aggregator counts an observation message whose request_count, sequence or window_seconds is outside the range schemas/observation.v1.json allows as malformed (observations_rejected) and skips it, instead of applying it, diverting it as window_too_long, or stopping and crash-looping on its redelivery (a negative request_count, or one large enough that the resulting window count cannot be encoded)

## Consequences

* **Code.** Two files change:
  * `packages/hammertime-core/src/hammertime/core/events/codec.py`, for
    decisions 1 and 2. Its module docstring cites this ADR.
  * `services/aggregator/src/hammertime/aggregator/transitions.py`, for
    decision 3. Its docstrings cite this ADR.

  The worker, ingest, the bus and the store do not change.
* **Tests.** Three files:
  * `test_codec.py`: both directions, every field, bounds read from the
    schema files, and the `10**4300 - 1` regression;
  * `test_worker.py`: out-of-range observations are `MALFORMED` end to end
    and are acknowledged, the consumer carries on, and the second trigger
    no longer fires;
  * `test_hysteresis.py`: an encode failure persists nothing.

  The tests that use numbers beyond 4,300 digits rely on CPython's default
  integer-string limit, as `test_codec.py` already does.
* **`CHANGES`.** One line (assumption 12), added by the implementing change.
* **Spec.** No text change. §30's `validate(message)` and §36's "extremely
  large numbers" are what this ADR implements on the bus side.
  `docs/spec/README.md`'s rows for §19 and §30/39 name this ADR.
* **ADR-0011 and ADR-0015** gain dated notes and one short amendment
  section each (next section).
* **Not done here.** The open items of assumption 11.

## Sources

* JSON Schema Validation, draft 2020-12,
  `json-schema.org/draft/2020-12/json-schema-validation`. I read it on
  2026-09-23 through a fetch tool that quotes and summarizes.
  * §6.2.2: "If the instance is a number, then this keyword validates only
    if the instance is less than or equal to "maximum"."
  * §6.2.4: "If the instance is a number, then this keyword validates only
    if the instance is greater than or exactly equal to "minimum"."

  From these I took that both bounds are inclusive. §6.1.1's definition of
  `"integer"` ("any number with a zero fractional part") was quoted again
  and matches ADR-0015 assumption 64's reading.
* CPython's integer-string limit: 4,300 digits by default, and nothing
  longer converts in either direction under it. Taken from ADR-0015
  assumption 61, which cites `pycore_long.h` and `longobject.c`. Not fetched
  again here.
* `max_deliver = -1` on the aggregator's durables: ADR-0013 decision 5 and
  assumption 9, and `packages/hammertime-bus/src/hammertime/bus/nats.py`,
  read in this repository.

## Edits to other documents

Nothing was removed anywhere. Each edit is an insertion:

* **ADR-0015.**
  * The status line gains a clause naming Amendment 4.
  * Amendment 3, ruling 3, the "*What is not changed.*" paragraph: after
    "Schema minimums and maximums are not added (below).", a dated note says
    that decisions 1 and 2 of this ADR add them, on decode and on encode.
  * Amendment 3, "Found while ruling, not ruled here", third bullet: a
    dated note says this ADR rules it.
  * A new "Amendment 4" section at the end lists these edits.
* **ADR-0011.**
  * The status line gains a clause naming Amendment 8.
  * Decision 3: a dated blockquote follows the 2026-09-21 note. It says
    what step 1's `MALFORMED` now covers and where `WINDOW_TOO_LONG` ends.
  * Decision 4: a dated blockquote follows step 5 and gives decision 3's
    order.
  * A new "Amendment 8" section at the end lists these edits.
* **`docs/spec/README.md`.**
  * The §19 row's last column was "`core/events`, `packages/hammertime-bus`
    (`interface.py`, `memory.py`, `nats.py`), `tools/provision`,
    `docs/adr/0004`, `docs/adr/0013`, `docs/adr/0015` (Amendment 3 ruling
    3: the codec's integer fields)". It gains "`docs/adr/0016` (decisions 1
    and 2: the schemas' numeric bounds)".
  * The §30/39 row's last column was "`services/aggregator/worker.py`,
    `services/aggregator/transitions.py`, `core/state/transitions.py`,
    `docs/adr/0011`". It gains "`docs/adr/0016` (decision 3: a transition is
    encoded before it is persisted)".
