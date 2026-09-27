// Pure per-day/per-handle classification logic, shared between the
// tracker page (loaded as a plain <script>) and the Node test suite
// (loaded via require()) - see scripts/tests/test_cell_status.mjs.
//
// Kept dependency-free and DOM-free on purpose: no fetch, no document,
// nothing that only exists in a browser. A day with no entry at all
// ("pending" - never checked, e.g. a future date) must never render the
// same as a confirmed "missed" day - that was a real bug (see
// scripts/check_posts.py's bucket_media_by_day docstring for the
// backend half of the same class of bug).
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.CellStatus = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function classifyCell(result) {
    if (!result) return { cls: "pending" };
    if (result.status === "error") {
      return { cls: "error", message: result.message || "unknown error" };
    }
    if (result.posted) {
      return { cls: "posted", permalink: result.permalink || null };
    }
    return { cls: "missed" };
  }

  function computeHandleStats(days, dates, handle) {
    let postedCount = 0;
    let checkedCount = 0;
    for (const d of dates) {
      const r = (days[d] || {})[handle];
      if (r && r.status !== "error") {
        checkedCount++;
        if (r.posted) postedCount++;
      }
    }
    return { postedCount, checkedCount };
  }

  function computeOverallStats(handles, days, dates) {
    let totalPosted = 0;
    let totalChecked = 0;
    let errorCount = 0;
    for (const h of handles) {
      for (const d of dates) {
        const r = (days[d] || {})[h];
        if (!r) continue;
        if (r.status === "error") {
          errorCount++;
        } else {
          totalChecked++;
          if (r.posted) totalPosted++;
        }
      }
    }
    const rate = totalChecked ? Math.round((totalPosted / totalChecked) * 100) : 0;
    const missedCount = totalChecked - totalPosted;
    return { totalPosted, totalChecked, errorCount, missedCount, rate };
  }

  function countHandlesPostedEveryDay(handles, days, dates) {
    let count = 0;
    for (const handle of handles) {
      const { postedCount, checkedCount } = computeHandleStats(days, dates, handle);
      // checkedCount > 0 excludes a handle with nothing checked yet in
      // this range from trivially counting as "posted every day" - zero
      // days checked is not a perfect streak, it's no data.
      if (checkedCount > 0 && postedCount === checkedCount) count++;
    }
    return count;
  }

  return { classifyCell, computeHandleStats, computeOverallStats, countHandlesPostedEveryDay };
});
