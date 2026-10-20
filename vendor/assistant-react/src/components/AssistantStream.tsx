import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { srOnly, usePrefersReducedMotion } from "../tokens";

export interface AssistantStreamProps {
  /** True while a turn is queued or running. */
  busy: boolean;
  /** Spoken once, when the turn is done ("Answer ready. Answered in 6.2 s"). */
  doneAnnouncement?: string | null;
  /** Changes when the stream grows; the view follows it to the end. */
  scrollKey?: string | number;
  label?: string;
  children: ReactNode;
}

/**
 * The transcript: role=log with aria-live polite. aria-busy holds
 * announcements while a turn streams, and the status line speaks only on done,
 * so assistive tech hears each answer once instead of every token.
 */
export function AssistantStream({ busy, doneAnnouncement, scrollKey, label = "Conversation", children }: AssistantStreamProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const reduced = usePrefersReducedMotion();
  useEffect(() => {
    const end = endRef.current;
    if (end && typeof end.scrollIntoView === "function") {
      end.scrollIntoView({ block: "end", behavior: reduced ? "auto" : "smooth" });
    }
  }, [scrollKey, reduced]);
  return (
    <>
      <div
        role="log"
        aria-live="polite"
        aria-relevant="additions text"
        aria-busy={busy}
        aria-label={label}
        data-testid="assistant-stream"
        style={{ display: "flex", flexDirection: "column", gap: 12 }}
      >
        {children}
        <div ref={endRef} aria-hidden="true" />
      </div>
      <div role="status" aria-live="polite" data-testid="assistant-announcer" style={srOnly}>
        {busy ? "" : doneAnnouncement ?? ""}
      </div>
    </>
  );
}
