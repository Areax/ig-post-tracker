// Tests for docs/cell-status.js - the frontend's day-cell classification.
// This is the display-side counterpart to the backend's coverage rule
// (scripts/tests/test_bucket_media_by_day.py): a day with NO entry at all
// must never render the same as a confirmed miss. That exact bug shipped
// once already (see git history) before this was extracted into its own
// testable module.
//
// Run with: node --test scripts/tests/test_cell_status.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const CellStatus = require("../../docs/cell-status.js");

test("no result at all classifies as pending, not missed", () => {
  assert.deepEqual(CellStatus.classifyCell(undefined), { cls: "pending" });
  assert.deepEqual(CellStatus.classifyCell(null), { cls: "pending" });
});

test("an error result classifies as error and carries the message", () => {
  const result = CellStatus.classifyCell({ status: "error", message: "rate limited" });
  assert.equal(result.cls, "error");
  assert.equal(result.message, "rate limited");
});

test("an error result with no message falls back to a generic one", () => {
  const result = CellStatus.classifyCell({ status: "error" });
  assert.equal(result.cls, "error");
  assert.equal(result.message, "unknown error");
});

test("a posted result classifies as posted and carries the permalink", () => {
  const result = CellStatus.classifyCell({ status: "ok", posted: true, permalink: "https://instagram.com/p/abc/" });
  assert.equal(result.cls, "posted");
  assert.equal(result.permalink, "https://instagram.com/p/abc/");
});

test("a posted result with no permalink still classifies as posted", () => {
  const result = CellStatus.classifyCell({ status: "ok", posted: true });
  assert.equal(result.cls, "posted");
  assert.equal(result.permalink, null);
});

test("a confirmed not-posted result classifies as missed", () => {
  const result = CellStatus.classifyCell({ status: "ok", posted: false });
  assert.deepEqual(result, { cls: "missed" });
});

test("computeHandleStats only counts ok entries, skipping errors and gaps", () => {
  const days = {
    "2026-08-17": { torch_boy: { status: "ok", posted: true } },
    "2026-08-18": { torch_boy: { status: "ok", posted: false } },
    "2026-08-19": { torch_boy: { status: "error", message: "boom" } },
    "2026-08-20": {}, // no data at all for this handle
  };
  const dates = Object.keys(days);

  const { postedCount, checkedCount } = CellStatus.computeHandleStats(days, dates, "torch_boy");

  assert.equal(postedCount, 1);
  assert.equal(checkedCount, 2, "error and missing days must not count toward the denominator");
});

test("computeOverallStats rate is based on checked days, not the full window", () => {
  const days = {
    "2026-08-17": { a: { status: "ok", posted: true } },
    "2026-08-18": { a: { status: "ok", posted: false } },
    "2026-08-19": {}, // pending for "a" - no entry at all - must not drag the rate down
  };
  const dates = Object.keys(days);

  const stats = CellStatus.computeOverallStats(["a"], days, dates);

  assert.equal(stats.totalChecked, 2);
  assert.equal(stats.totalPosted, 1);
  assert.equal(stats.rate, 50);
});

test("computeOverallStats counts errors separately from checked/posted", () => {
  const days = {
    "2026-08-17": { a: { status: "error", message: "boom" } },
  };
  const dates = Object.keys(days);

  const stats = CellStatus.computeOverallStats(["a"], days, dates);

  assert.equal(stats.errorCount, 1);
  assert.equal(stats.totalChecked, 0);
});

test("computeOverallStats rate is 0 (not NaN) when nothing has been checked yet", () => {
  const stats = CellStatus.computeOverallStats(["a"], {}, []);
  assert.equal(stats.rate, 0);
});
