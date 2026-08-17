export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  streaming?: boolean;
}

export interface RawSseEvent {
  eventType: string;
  data: unknown;
  raw: string;
  timestamp: number;
}

export interface StreamCallbacks {
  onTextDelta: (delta: string) => void;
  onToolCalled: (toolName: string) => void;
  onDone: () => void;
  onError: (err: Error) => void;
}

const API = {
  chat: '/chat',
  chatStop: '/stop',
  history: '/history',
  clearHistory: '/clear-history',
  conversations: '/conversations',
  deleteConversation: '/delete-conversation',
};

function dispatchSseChunk(
  part: string,
  cb: StreamCallbacks,
  markDone: () => void,
): void {
  let eventType = '';
  let data = '';
  for (const line of part.split('\n')) {
    if (line.startsWith('event: ')) eventType = line.slice(7);
    else if (line.startsWith('data: ')) data = line.slice(6);
  }
  if (!eventType || !data) return;
  try {
    const parsed = JSON.parse(data);
    switch (eventType) {
      case 'text_delta':
        cb.onTextDelta(parsed.delta);
        break;
      case 'tool_called':
        cb.onToolCalled(parsed.tool);
        break;
      case 'error':
        cb.onError(new Error(parsed.message || 'error'));
        break;
      case 'done':
        markDone();
        cb.onDone();
        break;
    }
  } catch {}
}

export function sendMessageStream(
  message: string,
  callbacks: StreamCallbacks,
  conversationId?: string,
): AbortController {
  const ctrl = new AbortController();
  (async () => {
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (conversationId) headers['makers-conversation-id'] = conversationId;
      const res = await fetch(API.chat, {
        method: 'POST',
        headers,
        body: JSON.stringify({ message }),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        callbacks.onError(new Error(`HTTP ${res.status}`));
        return;
      }
      const reader = res.body?.getReader();
      if (!reader) { callbacks.onError(new Error('no stream')); return; }
      const decoder = new TextDecoder();
      let buffer = '';
      let doneReceived = false;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';
        for (const part of parts) {
          if (!part.trim()) continue;
          dispatchSseChunk(part, callbacks, () => { doneReceived = true; });
        }
      }
      if (!doneReceived) callbacks.onDone();
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      callbacks.onError(err instanceof Error ? err : new Error(String(err)));
    }
  })();
  return ctrl;
}

export async function fetchConversationHistory(conversationId: string): Promise<Message[]> {
  try {
    const res = await fetch(API.history, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId }),
    });
    if (!res.ok) return [];
    const data = await res.json().catch(() => null) as { messages?: Message[] } | null;
    return Array.isArray(data?.messages) ? data.messages : [];
  } catch { return []; }
}

export async function stopAgent(conversationId?: string): Promise<boolean> {
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (conversationId) headers['makers-conversation-id'] = conversationId;
    const res = await fetch(API.chatStop, {
      method: 'POST',
      headers,
      body: JSON.stringify({ conversation_id: conversationId }),
    });
    return res.ok;
  } catch { return false; }
}

export async function clearConversationHistory(conversationId: string): Promise<boolean> {
  try {
    const res = await fetch(API.clearHistory, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId }),
    });
    return res.ok;
  } catch { return false; }
}
