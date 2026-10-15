/**
 * FlushTracker: the auto-flush cadence that makes OpenCode save like Hermes.
 *
 * Pure unit tests against the compiled tracker — no sidecar, no database.
 *
 *   npm test
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DEFAULT_FLUSH_MESSAGE, FlushTracker } from "../dist/src/flush.js";

const base = {
  flushEnabled: true,
  flushMinTurns: 6,
  nudgeInterval: 10,
  flushMessage: "",
};

function tracker(overrides = {}) {
  return new FlushTracker({ ...base, ...overrides });
}

/** Push `count` distinct user turns through the tracker. */
function turns(tracker, session, count) {
  let last = "";
  for (let index = 1; index <= count; index++) {
    last = tracker.reminderFor(session, `turn-${index}`);
  }
  return last;
}

describe("FlushTracker cadence", () => {
  it("stays quiet below the threshold", () => {
    const flush = tracker();
    assert.equal(turns(flush, "s1", base.flushMinTurns - 1), "");
    assert.equal(flush.turnsSinceSave("s1"), base.flushMinTurns - 1);
  });

  it("fires at flushMinTurns and returns the default message", () => {
    const flush = tracker();
    const text = turns(flush, "s1", base.flushMinTurns);
    assert.notEqual(text, "");
    assert.equal(text, DEFAULT_FLUSH_MESSAGE);
  });

  it("does not repeat on every turn after the first nudge", () => {
    const flush = tracker();
    turns(flush, "s1", base.flushMinTurns);
    // A few more turns, less than nudgeInterval since the first reminder.
    for (let index = 0; index < 4; index++) {
      const text = flush.reminderFor("s1", `after-${index}`);
      assert.equal(text, "");
    }
    assert.equal(flush.turnsSinceSave("s1"), base.flushMinTurns + 4);
  });

  it("nudges again once nudgeInterval turns pass without a save", () => {
    const flush = tracker();
    turns(flush, "s1", base.flushMinTurns);
    // First nudge happened at turn 6; after 10 more turns it is due again.
    let fired = 0;
    for (let index = 1; index <= base.nudgeInterval; index++) {
      if (flush.reminderFor("s1", `more-${index}`) !== "") fired += 1;
    }
    assert.equal(fired, 1);
  });

  it("counts a turn once even when the hook re-runs for a tool call", () => {
    const flush = tracker();
    for (let index = 0; index < base.flushMinTurns; index++) {
      // Same turn key on every call: the context hook runs per continuation.
      assert.equal(flush.reminderFor("s1", "same-turn"), "");
    }
    assert.equal(flush.turnsSinceSave("s1"), 1);
    assert.equal(flush.reminderFor("s1", "same-turn"), "");
    assert.equal(flush.reminderFor("s1", "next-turn"), "");
    assert.equal(flush.turnsSinceSave("s1"), 2);
  });

  it("restarts the cadence after a save", () => {
    const flush = tracker();
    turns(flush, "s1", base.flushMinTurns);
    flush.markSaved("s1");
    assert.equal(flush.turnsSinceSave("s1"), 0);
    // The threshold must be reached again before another reminder.
    assert.equal(turns(flush, "s1", base.flushMinTurns - 1), "");
    assert.notEqual(flush.reminderFor("s1", `turn-${base.flushMinTurns}`), "");
  });

  it("keeps sessions independent", () => {
    const flush = tracker();
    turns(flush, "s-a", base.flushMinTurns);
    assert.equal(flush.turnsSinceSave("s-b"), 0);
    assert.equal(flush.reminderFor("s-b", "first"), "");
    assert.equal(flush.turnsSinceSave("s-a"), base.flushMinTurns);
  });
});

describe("FlushTracker options", () => {
  it("stays silent when flushing is disabled", () => {
    const flush = tracker({ flushEnabled: false });
    assert.equal(turns(flush, "s1", 50), "");
    assert.equal(flush.turnsSinceSave("s1"), 0);
  });

  it("uses a custom message when provided", () => {
    const flush = tracker({ flushMessage: "guardá los facts" });
    assert.equal(turns(flush, "s1", base.flushMinTurns), "guardá los facts");
  });

  it("honours a lower threshold", () => {
    const flush = tracker({ flushMinTurns: 2 });
    assert.equal(flush.reminderFor("s1", "t1"), "");
    assert.notEqual(flush.reminderFor("s1", "t2"), "");
  });

  it("ignores a save for a session it never tracked", () => {
    const flush = tracker();
    assert.doesNotThrow(() => flush.markSaved("never-seen"));
  });
});
