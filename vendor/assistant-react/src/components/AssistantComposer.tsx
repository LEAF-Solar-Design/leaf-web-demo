import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { TurnRequestV1, clientReason } from "../../../assistant-contract/src/index";
import type { ReasonV1 } from "../../../assistant-contract/src/index";
import { newRequestId, reasonCopy } from "../states";
import { color, figure, meta, primaryButton, radius, secondaryButton, target } from "../tokens";

/** The counter starts at 7,000, the most TurnRequestV1 accepts. */
export const COMPOSER_LIMIT = 7000;
/** A send that has not settled by then is Not sent with the client's own timeout copy. */
export const SEND_TIMEOUT_MS = 15_000;

export type SendOutcome = { readonly ok: true } | { readonly ok: false; readonly reason: ReasonV1 };
/** Posts a TurnRequestV1. Resolve `{ok:false, reason}` for a refusal (429 and friends); a throw reads as offline. */
export type ComposerSend = (request: { request_id: string; text: string }) => Promise<SendOutcome | void>;
export type SendStatus = "idle" | "sending" | "sent" | "not_sent";

export interface AssistantComposerProps {
  onSend: ComposerSend;
  disabled?: boolean;
  timeoutMs?: number;
  /** Defaults to navigator.onLine. */
  isOnline?: () => boolean;
  textareaRef?: { readonly current: HTMLTextAreaElement | null };
  /** A starter the user picked: fills the draft. `nonce` changes per pick. */
  seed?: { readonly text: string; readonly nonce: number } | null;
}

function navigatorOnline(): boolean {
  return typeof navigator === "undefined" || navigator.onLine !== false;
}

function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

/**
 * Optimistic send: Sending, then Sent, or Not sent with the reason and the
 * draft put back. A retry of the same text reuses its request_id, so a send
 * that landed just before a timeout is never asked twice. Every send is
 * bounded by `timeoutMs`; a late answer to an abandoned send is ignored.
 */
export function AssistantComposer({
  onSend,
  disabled = false,
  timeoutMs = SEND_TIMEOUT_MS,
  isOnline = navigatorOnline,
  textareaRef,
  seed,
}: AssistantComposerProps) {
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<SendStatus>("idle");
  const [reason, setReason] = useState<ReasonV1 | null>(null);
  const localRef = useRef<HTMLTextAreaElement>(null);
  const boxRef = textareaRef ?? localRef;
  const attempt = useRef(0);
  const lastRequest = useRef<{ id: string; text: string } | null>(null);
  const mounted = useRef(true);
  const counterId = useId();

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const seedNonce = seed?.nonce;
  useEffect(() => {
    if (!seed) return;
    setDraft(seed.text);
    boxRef.current?.focus();
    // Only a new pick re-seeds; the text of the same pick never overwrites later edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedNonce]);

  const remaining = COMPOSER_LIMIT - draft.length;
  const canSend = !disabled && status !== "sending" && draft.trim().length > 0 && remaining >= 0;

  const send = useCallback(async () => {
    const text = draft.trim();
    if (disabled || status === "sending" || text.length === 0 || text.length > COMPOSER_LIMIT) return;
    if (!isOnline()) {
      setReason(clientReason("client.offline"));
      setStatus("not_sent");
      return;
    }
    const previous = lastRequest.current;
    const id = previous !== null && previous.text === text ? previous.id : newRequestId();
    const request = TurnRequestV1.safeParse({ request_id: id, text });
    if (!request.success) return;
    lastRequest.current = { id, text };
    const mine = ++attempt.current;
    setStatus("sending");
    setReason(null);
    setDraft("");

    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<SendOutcome>((resolve) => {
      timer = setTimeout(() => resolve({ ok: false, reason: clientReason("client.timeout") }), timeoutMs);
    });
    let outcome: SendOutcome;
    try {
      const sent = onSend(request.data).then((result): SendOutcome => result ?? { ok: true });
      outcome = await Promise.race([sent, timeout]);
    } catch {
      outcome = { ok: false, reason: clientReason("client.offline") };
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
    if (!mounted.current || mine !== attempt.current) return;
    if (outcome.ok) {
      lastRequest.current = null;
      setStatus("sent");
      return;
    }
    const refused = outcome.reason;
    setReason(refused);
    setStatus("not_sent");
    setDraft((current) => (current.length === 0 ? text : current));
  }, [draft, disabled, status, isOnline, onSend, timeoutMs]);

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void send();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  };

  const notSent = status === "not_sent" && reason !== null ? reasonCopy(reason) : null;
  const counter = remaining >= 0 ? `${formatCount(remaining)} left` : `Over by ${formatCount(-remaining)}`;

  return (
    <form aria-label="Ask the assistant" onSubmit={onSubmit} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <textarea
        ref={boxRef}
        data-testid="assistant.prompt"
        aria-label="Message the assistant"
        aria-describedby={counterId}
        rows={2}
        value={draft}
        disabled={disabled}
        onChange={(event) => {
          setDraft(event.target.value);
          if (status === "sent") setStatus("idle");
        }}
        onKeyDown={onKeyDown}
        style={{
          ...target,
          width: "100%",
          resize: "vertical",
          padding: "10px 12px",
          font: "inherit",
          border: `1px solid ${color.input}`,
          borderRadius: radius,
          background: color.card,
          color: color.foreground,
        }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span
          id={counterId}
          data-testid="assistant-counter"
          style={{ ...figure, ...meta, color: remaining < 0 ? color["state-critical"] : color["muted-foreground"] }}
        >
          {counter}
        </span>
        <span role="status" aria-live="polite" data-testid="assistant-send-status" style={{ flex: 1, minWidth: 0 }}>
          {status === "sending" ? "Sending" : null}
          {status === "sent" ? "Sent" : null}
          {notSent ? (
            <>
              <strong>Not sent</strong> <span>{notSent.text}</span> <span>Your draft is back in the box.</span>
            </>
          ) : null}
        </span>
        {notSent ? (
          <button
            type="button"
            style={secondaryButton}
            onClick={() => {
              if (notSent.action === "retry") {
                void send();
                return;
              }
              setStatus("idle");
              setReason(null);
            }}
          >
            {notSent.label}
          </button>
        ) : null}
        <button type="submit" style={primaryButton} disabled={!canSend}>
          Send
        </button>
      </div>
    </form>
  );
}
