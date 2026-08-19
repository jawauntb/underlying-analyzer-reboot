import type { AgentStreamEvent, TransportStatus } from './contracts';
import { exactMobileToolEcho, MOBILE_AGENT_TOOLS } from './agentTools';
import { isRecord } from './guards';

export const MAX_NDJSON_RECORD_BYTES = 256 * 1024;

export class NdjsonProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NdjsonProtocolError';
  }
}

function concatBytes(left: Uint8Array, right: Uint8Array): Uint8Array {
  if (!left.length) return right.slice();
  if (!right.length) return left;
  const joined = new Uint8Array(left.length + right.length);
  joined.set(left);
  joined.set(right, left.length);
  return joined;
}

export class NdjsonParser {
  private carry = new Uint8Array();
  private readonly decoder = new TextDecoder('utf-8', { fatal: true });
  private aborted = false;
  private finished = false;

  constructor(private readonly onRecord: (value: unknown) => void) {}

  push(chunk: Uint8Array): void {
    if (this.aborted || this.finished || chunk.length === 0) return;
    const bytes = concatBytes(this.carry, chunk);
    let start = 0;
    for (let index = 0; index < bytes.length; index += 1) {
      if (bytes[index] !== 10) continue;
      let end = index;
      if (end > start && bytes[end - 1] === 13) end -= 1;
      this.parseRecord(bytes.slice(start, end));
      start = index + 1;
    }
    this.carry = bytes.slice(start);
    if (this.carry.length > MAX_NDJSON_RECORD_BYTES) {
      this.carry = new Uint8Array();
      throw new NdjsonProtocolError('NDJSON carry exceeds the 256 KiB limit.');
    }
  }

  finish(): void {
    if (this.aborted || this.finished) return;
    this.finished = true;
    if (this.carry.length) this.parseRecord(this.carry);
    this.carry = new Uint8Array();
  }

  abort(): void {
    this.aborted = true;
    this.carry = new Uint8Array();
  }

  private parseRecord(bytes: Uint8Array): void {
    if (bytes.length > MAX_NDJSON_RECORD_BYTES) {
      throw new NdjsonProtocolError('NDJSON record exceeds the 256 KiB limit.');
    }
    let line: string;
    try {
      line = this.decoder.decode(bytes);
    } catch {
      throw new NdjsonProtocolError('NDJSON record contains invalid UTF-8.');
    }
    if (!line.trim()) return;
    try {
      this.onRecord(JSON.parse(line));
    } catch (error) {
      if (error instanceof NdjsonProtocolError) throw error;
      throw new NdjsonProtocolError('NDJSON record is not valid JSON.');
    }
  }
}

type AgentSnapshot = {
  status: Extract<TransportStatus, 'streaming' | 'completed' | 'error' | 'cancelled'>;
  text: string;
  model: string | null;
  tools: string[];
  events: AgentStreamEvent[];
  error: string | null;
};

function requiredString(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value) throw new NdjsonProtocolError(`${label} must be a string.`);
  return value;
}

function exactToolSet(value: unknown): string[] {
  const tools = exactMobileToolEcho(value);
  if (!tools) {
    throw new NdjsonProtocolError('Agent tool allowlist mismatch.');
  }
  return tools;
}

function sanitizeArtifact(artifact: unknown): Record<string, unknown> | null {
  if (!isRecord(artifact)) return null;
  return Object.fromEntries(
    Object.entries(artifact).filter(
      ([key]) => !['data', 'base64', 'bytes', 'content'].includes(key.toLowerCase()),
    ),
  );
}

function normalizeEvent(value: unknown): AgentStreamEvent {
  if (!isRecord(value)) throw new NdjsonProtocolError('Agent event must be an object.');
  const type = requiredString(value.type, 'Agent event type');
  switch (type) {
    case 'start':
      return {
        type,
        model: requiredString(value.model, 'Agent model'),
        tools: exactToolSet(value.tools),
      };
    case 'text':
      return { type, text: typeof value.text === 'string' ? value.text : '' };
    case 'tool_call': {
      const name = requiredString(value.name, 'Tool name');
      if (!(MOBILE_AGENT_TOOLS as readonly string[]).includes(name)) {
        throw new NdjsonProtocolError(`Tool ${name} is not allowed by the mobile client.`);
      }
      return {
        type,
        id: requiredString(value.id, 'Tool call id'),
        name,
        title: typeof value.title === 'string' ? value.title : undefined,
        group: typeof value.group === 'string' ? value.group : undefined,
        cost: typeof value.cost === 'string' ? value.cost : undefined,
        input: isRecord(value.input) ? value.input : {},
      };
    }
    case 'tool_result': {
      const name = requiredString(value.name, 'Tool result name');
      if (!(MOBILE_AGENT_TOOLS as readonly string[]).includes(name)) {
        throw new NdjsonProtocolError(`Tool ${name} is not allowed by the mobile client.`);
      }
      return {
        type,
        id: requiredString(value.id, 'Tool result id'),
        name,
        ok: value.ok === true,
        status: typeof value.status === 'number' ? value.status : undefined,
        durationMs: typeof value.duration_ms === 'number' ? value.duration_ms : undefined,
        result: value.result,
        artifacts: Array.isArray(value.artifacts)
          ? value.artifacts.flatMap((artifact) => {
              const safe = sanitizeArtifact(artifact);
              return safe ? [safe] : [];
            })
          : [],
        error: typeof value.error === 'string' ? value.error : undefined,
      };
    }
    case 'article':
      if (!isRecord(value.article)) throw new NdjsonProtocolError('Article event is missing article data.');
      return {
        type,
        article: value.article,
        markdown: typeof value.markdown === 'string' ? value.markdown : undefined,
      };
    case 'error':
      return { type, message: requiredString(value.message, 'Agent error') };
    case 'done':
      return {
        type,
        stopReason: typeof value.stop_reason === 'string' ? value.stop_reason : 'end_turn',
        text: typeof value.text === 'string' ? value.text : '',
        toolTrace: Array.isArray(value.tool_trace) ? value.tool_trace.map(String) : [],
      };
    default:
      throw new NdjsonProtocolError(`Unknown agent event type: ${type}.`);
  }
}

export class AgentStreamState {
  private state: AgentSnapshot = {
    status: 'streaming',
    text: '',
    model: null,
    tools: [],
    events: [],
    error: null,
  };
  private started = false;
  private readonly textChunks: string[] = [];

  accept(value: unknown): AgentStreamEvent | null {
    if (this.state.status === 'cancelled') return null;
    if (this.state.status === 'completed' || this.state.status === 'error') {
      throw new NdjsonProtocolError('Agent stream emitted data after a terminal event.');
    }
    const event = normalizeEvent(value);
    if (!this.started && event.type !== 'start') {
      throw new NdjsonProtocolError('Agent stream must begin with a start event.');
    }
    if (this.started && event.type === 'start') {
      throw new NdjsonProtocolError('Agent stream emitted more than one start event.');
    }
    if (event.type === 'start') {
      this.started = true;
      this.state.model = event.model;
      this.state.tools = event.tools;
    } else if (event.type === 'text') {
      this.textChunks.push(event.text);
    } else if (event.type === 'error') {
      this.state.status = 'error';
      this.state.error = event.message;
    } else if (event.type === 'done') {
      this.state.status = 'completed';
      if (this.textChunks.length === 0 && event.text) this.textChunks.push(event.text);
    }
    if (event.type === 'tool_call') {
      this.state.events.push({ ...event, input: {} });
    } else if (event.type === 'tool_result') {
      this.state.events.push({
        type: event.type,
        id: event.id,
        name: event.name,
        ok: event.ok,
        status: event.status,
        durationMs: event.durationMs,
        artifacts: event.artifacts,
        error: event.error,
      });
    }
    return event;
  }

  finish(): AgentSnapshot {
    if (this.state.status === 'cancelled' || this.state.status === 'completed') return this.snapshot();
    if (this.state.status === 'error') throw new NdjsonProtocolError(this.state.error ?? 'Agent stream failed.');
    this.state.status = 'error';
    this.state.error = 'Agent stream ended without a terminal done or error event.';
    throw new NdjsonProtocolError(this.state.error);
  }

  fail(message: string): void {
    if (this.state.status !== 'streaming') return;
    this.state.status = 'error';
    this.state.error = message;
  }

  cancel(): void {
    if (this.state.status === 'streaming') this.state.status = 'cancelled';
  }

  snapshot(): AgentSnapshot {
    return {
      ...this.state,
      text: this.textChunks.join(''),
      tools: [...this.state.tools],
      events: [...this.state.events],
    };
  }
}
