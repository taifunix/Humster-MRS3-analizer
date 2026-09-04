(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.retestRecovery = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  const RECOVERY_TERMINAL = new Set(['FAILED', 'CANCELLED', 'SUCCEEDED']);
  const isRecoveryTerminal = (job) => RECOVERY_TERMINAL.has(job?.state);
  const selectRetestTester = (jobs) => {
    const retestTesters = (Array.isArray(jobs) ? jobs : [])
      .filter((job) => job?.kind === 'strategies.tester.native.start' && job?.retest === true)
      .reverse();
    return retestTesters.find((job) => job.state === 'COMMITTED' && job.inbox_ready === true)
      || retestTesters.find((job) => !isRecoveryTerminal(job))
      || retestTesters.find((job) => isRecoveryTerminal(job))
      || null;
  };
  const selectCommittedRetestTester = (jobs) => (Array.isArray(jobs) ? jobs : [])
    .filter((job) => job?.kind === 'strategies.tester.native.start' && job?.retest === true && job?.state === 'COMMITTED')
    .reverse()[0] || null;
  return { isRecoveryTerminal, selectRetestTester, selectCommittedRetestTester };
});
