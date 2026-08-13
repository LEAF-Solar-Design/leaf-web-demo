/** Framework-neutral browser model for request-backed conversation workspaces. */

export interface BrowserConversationMessage {
  seq: number;
  request_id: string;
  role: string;
  kind: string;
  text: string;
  ts: string;
}

export interface BrowserConversation {
  id: string;
  title: string;
  state: string;
  active: boolean;
  updated_at: string;
  request_ids: string[];
  messages: BrowserConversationMessage[];
}

export interface ConversationWorkspaceState {
  conversations: BrowserConversation[];
  selectedId: string | null;
}

/** Merge a polled snapshot without losing the user's selected thread. */
export function mergeConversationSnapshot(
  previous: ConversationWorkspaceState,
  incoming: BrowserConversation[],
): ConversationWorkspaceState {
  const conversations = incoming
    .map((conversation) => structuredClone(conversation))
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  // A selected ID may be a client-side draft that has not produced its first
  // durable server event yet. Polling must not silently switch that draft to
  // another active conversation.
  const selectedId = previous.selectedId ?? conversations[0]?.id ?? null;
  return { conversations, selectedId };
}

export function selectConversation(
  state: ConversationWorkspaceState,
  id: string,
): ConversationWorkspaceState {
  if (!state.conversations.some((conversation) => conversation.id === id)) return state;
  return { conversations: state.conversations, selectedId: id };
}

export function selectedConversation(
  state: ConversationWorkspaceState,
): BrowserConversation | undefined {
  return state.conversations.find((conversation) => conversation.id === state.selectedId);
}

export function conciseConversationTitle(prompt: string, maxLength = 56): string {
  const title = String(prompt).replace(/\s+/g, " ").trim() || "New conversation";
  return title.length <= maxLength ? title : `${title.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
}
