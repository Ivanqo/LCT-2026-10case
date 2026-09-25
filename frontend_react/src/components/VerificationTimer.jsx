import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { fmtDuration, parseUtc } from '../utils.js';

/**
 * Verification time (expert session §35): from opening the protocol to its finalization. Opening the workbench
 * starts the clock on the server (once per verification cycle); finalization freezes the number into the audit log.
 */
export default function VerificationTimer({ processId, status, refreshKey }) {
  const [timing, setTiming] = useState(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!processId) return;
    const call = status === 'FINALIZED' ? api.verificationTiming(processId) : api.verificationOpen(processId);
    call.then(setTiming).catch(() => setTiming(null));
  }, [processId, status, refreshKey]);

  useEffect(() => {
    if (!timing?.running) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [timing?.running]);

  if (!timing || !timing.opened_at) return null;
  const opened = parseUtc(timing.opened_at);
  const seconds = timing.running && opened ? (now - opened.getTime()) / 1000 : timing.duration_seconds;
  const last = (timing.measurements || []).slice(-1)[0];
  return (
    <div className={`verification-timer${timing.running ? ' running' : ''}`} title="От открытия протокола до финализации; пишется в журнал аудита">
      <div className="t-label">{timing.running ? 'Идёт верификация' : 'Время верификации'}</div>
      <div className="t-value mono">{fmtDuration(seconds)}</div>
      <div className="t-sub">
        решений {timing.decisions}
        {timing.avg_actions_per_decision ? ` • ${timing.avg_actions_per_decision} действ./реш.` : ''}
        {timing.avg_seconds_per_decision ? ` • ${timing.avg_seconds_per_decision} с/реш.` : ''}
      </div>
      {!timing.running && last && <div className="t-sub">зафиксировано в журнале</div>}
    </div>
  );
}
