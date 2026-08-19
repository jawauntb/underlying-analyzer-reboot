import {
  AgentStreamState,
  NdjsonParser,
  NdjsonProtocolError,
} from '@/src/api/ndjson';
import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';

const encoder = new TextEncoder();

function eventLine(value: unknown, ending = '\n') {
  return encoder.encode(`${JSON.stringify(value)}${ending}`);
}

describe('NdjsonParser', () => {
  it('decodes a UTF-8 record split at every byte boundary', () => {
    const bytes = eventLine({ type: 'text', text: 'price → café 📈' });
    for (let split = 1; split < bytes.length; split += 1) {
      const values: unknown[] = [];
      const parser = new NdjsonParser((value) => values.push(value));
      parser.push(bytes.slice(0, split));
      parser.push(bytes.slice(split));
      parser.finish();
      expect(values).toEqual([{ type: 'text', text: 'price → café 📈' }]);
    }
  });

  it('accepts split delimiters, CRLF, leading proxy padding, multiple records, and EOF carry', () => {
    const values: unknown[] = [];
    const parser = new NdjsonParser((value) => values.push(value));
    parser.push(encoder.encode('\n'.repeat(4096)));
    parser.push(encoder.encode('{"type":"text","text":"a"}\r'));
    parser.push(encoder.encode('\n{"type":"text","text":"b"}\n{"type":"done"}'));
    parser.finish();
    expect(values).toEqual([
      { type: 'text', text: 'a' },
      { type: 'text', text: 'b' },
      { type: 'done' },
    ]);
  });

  it('rejects malformed JSON with a typed protocol error', () => {
    const parser = new NdjsonParser(() => undefined);
    expect(() => parser.push(encoder.encode('{bad}\n'))).toThrow(NdjsonProtocolError);
  });

  it('rejects records and undelimited carry over 256 KiB', () => {
    const parser = new NdjsonParser(() => undefined);
    expect(() => parser.push(encoder.encode('x'.repeat(256 * 1024 + 1)))).toThrow(
      /256 KiB/,
    );

    const oversizedRecord = new NdjsonParser(() => undefined);
    expect(() =>
      oversizedRecord.push(encoder.encode(`${' '.repeat(256 * 1024 + 1)}\n`)),
    ).toThrow(/256 KiB/);
  });

  it('drops an incomplete carry when aborted', () => {
    const values: unknown[] = [];
    const parser = new NdjsonParser((value) => values.push(value));
    parser.push(encoder.encode('{"type":"text","text":"partial'));
    parser.abort();
    parser.finish();
    expect(values).toEqual([]);
  });
});

describe('AgentStreamState', () => {
  const start = { type: 'start', model: 'test', tools: [...MOBILE_AGENT_TOOLS] };

  it('requires an exact echoed allowlist as a set and reaches done', () => {
    const state = new AgentStreamState();
    state.accept({ ...start, tools: [...MOBILE_AGENT_TOOLS].reverse() });
    state.accept({ type: 'text', text: null });
    state.accept({ type: 'text', text: 'Ready.' });
    state.accept({ type: 'done', stop_reason: 'end_turn', text: 'Ready.', tool_trace: [] });
    expect(state.snapshot()).toMatchObject({ status: 'completed', text: 'Ready.' });
  });

  it.each([
    [MOBILE_AGENT_TOOLS.slice(0, -1)],
    [[...MOBILE_AGENT_TOOLS, 'render_chart']],
    [[...MOBILE_AGENT_TOOLS, MOBILE_AGENT_TOOLS[0]]],
    [['unknown']],
  ])('rejects an echoed start allowlist mismatch: %p', (tools) => {
    const state = new AgentStreamState();
    expect(() => state.accept({ ...start, tools })).toThrow(/tool allowlist mismatch/i);
  });

  it('rejects an out-of-set tool call and strips artifact base64 from results', () => {
    const state = new AgentStreamState();
    state.accept(start);
    expect(() =>
      state.accept({ type: 'tool_call', id: '1', name: 'render_chart', input: {} }),
    ).toThrow(/not allowed/i);

    const safeState = new AgentStreamState();
    safeState.accept(start);
    safeState.accept({
      type: 'tool_result',
      id: '1',
      name: 'chart_data',
      ok: true,
      result: { value: 1 },
      artifacts: [{ mime: 'image/png', data: 'base64-secret', filename: 'chart.png' }],
    });
    const snapshot = safeState.snapshot();
    expect(JSON.stringify(snapshot)).not.toContain('base64-secret');
    expect(snapshot.events[0]).not.toHaveProperty('result');
  });

  it('joins streamed text on read and retains only compact tool metadata', () => {
    const state = new AgentStreamState();
    state.accept(start);
    state.accept({ type: 'text', text: 'First ' });
    state.accept({ type: 'text', text: 'second.' });
    const call = state.accept({
      type: 'tool_call',
      id: '1',
      name: 'chart_data',
      input: { payload: 'x'.repeat(10_000) },
    });
    state.accept({
      type: 'tool_result',
      id: '1',
      name: 'chart_data',
      ok: true,
      result: { payload: 'x'.repeat(10_000) },
      artifacts: [{ title: 'Chart metadata' }],
    });
    state.accept({ type: 'done', text: 'First second.', stop_reason: 'end_turn' });

    expect(call).toMatchObject({ input: { payload: expect.any(String) } });
    expect(state.snapshot()).toMatchObject({
      text: 'First second.',
      events: [
        { type: 'tool_call', input: {} },
        { type: 'tool_result', artifacts: [{ title: 'Chart metadata' }] },
      ],
    });
    expect(state.snapshot().events[1]).not.toHaveProperty('result');
  });

  it('treats error as terminal, failed tool_result as nonterminal, and missing terminal as failure', () => {
    const failedTool = new AgentStreamState();
    failedTool.accept(start);
    failedTool.accept({ type: 'tool_result', id: '1', name: 'stock_fax', ok: false, error: 'no data' });
    expect(failedTool.snapshot().status).toBe('streaming');
    failedTool.accept({ type: 'error', message: 'agent stopped' });
    expect(failedTool.snapshot()).toMatchObject({ status: 'error', error: 'agent stopped' });

    const interrupted = new AgentStreamState();
    interrupted.accept(start);
    interrupted.accept({ type: 'text', text: 'partial' });
    expect(() => interrupted.finish()).toThrow(/without a terminal/i);
    expect(interrupted.snapshot().status).toBe('error');
  });

  it('models local cancellation without accepting late events', () => {
    const state = new AgentStreamState();
    state.accept(start);
    state.cancel();
    state.accept({ type: 'text', text: 'late' });
    expect(state.snapshot()).toMatchObject({ status: 'cancelled', text: '' });
  });
});
