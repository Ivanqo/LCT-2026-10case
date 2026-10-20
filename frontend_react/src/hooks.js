import { useEffect } from 'react';

/** Close a dialog with Esc (checklist: the keyboard must work everywhere); `enabled=false` while an action is running. */
export function useEscape(handler, enabled = true) {
  useEffect(() => {
    if (!enabled) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        handler();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [handler, enabled]);
}
