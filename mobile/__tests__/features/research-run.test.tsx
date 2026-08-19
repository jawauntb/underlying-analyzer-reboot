import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import { StrictMode } from 'react';
import { StyleSheet } from 'react-native';

import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import type { LibraryRecord } from '@/src/features/library/library-store';
import ResearchRunScreen from '@/src/features/research/ResearchRunScreen';
import { RESEARCH_MESSAGE } from '@/src/features/research/research-model';

jest.mock('@expo/vector-icons/Ionicons', () => {
  const React = jest.requireActual('react');
  const { Text } = jest.requireActual('react-native');
  return function MockIonicon({ name }: { name: string }) {
    return React.createElement(Text, null, name);
  };
});

jest.mock('react-native-safe-area-context', () => {
  const React = jest.requireActual('react');
  const { View } = jest.requireActual('react-native');
  return { SafeAreaView: ({ children, ...props }: { children?: React.ReactNode }) => React.createElement(View, props, children) };
});

const tool = (name: string) => ({
  name,
  title: name,
  group: 'research',
  summary: '',
  whenToUse: '',
  returns: '',
  cost: 'low',
  producesImages: false,
  agent: true,
  mcp: false,
  http: { method: 'POST', path: `/api/${name}` },
  arguments: [],
  required: [],
});

const catalog = {
  agentReady: true,
  model: 'claude-sonnet',
  toolCount: MOBILE_AGENT_TOOLS.length,
  tools: MOBILE_AGENT_TOOLS.map(tool),
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

type EventHandler = (event: unknown) => void;

function completedState(text = 'Completed AAPL research') {
  return {
    transport: 'stream' as const,
    state: {
      status: 'completed' as const,
      text,
      model: 'claude-sonnet',
      tools: [...MOBILE_AGENT_TOOLS],
      events: [
        { type: 'start', model: 'claude-sonnet', tools: [...MOBILE_AGENT_TOOLS] },
        { type: 'tool_call', id: 'call-1', name: 'analyze_ticker', input: {} },
        { type: 'tool_result', id: 'call-1', name: 'analyze_ticker', ok: true, durationMs: 31, artifacts: [] },
        { type: 'done', stopReason: 'end_turn', text, toolTrace: ['analyze_ticker'] },
      ],
      error: null,
    },
  };
}

function dependencies() {
  const client = {
    tools: jest.fn(async () => catalog),
    agentStream: jest.fn(),
  };
  const library = {
    save: jest.fn(async (value) => ({ record: { id: 'run-1', ...value }, prunedCount: 0 })),
    read: jest.fn(async (_id: string): Promise<LibraryRecord | null> => null),
  };
  const router = { back: jest.fn() };
  return {
    client,
    library,
    router,
    props: {
      client: client as never,
      library: library as never,
      router,
      reachability: 'online' as const,
      symbol: 'AAPL',
      period: '1y' as const,
      now: () => 500,
    },
  };
}

describe('ResearchRunScreen', () => {
  it('does only a safe capability read on open and previews agent_ready plus the exact six tools', async () => {
    const deps = dependencies();
    render(<ResearchRunScreen {...deps.props} />);

    expect(await screen.findByText('Research access is ready.')).toBeTruthy();
    expect(deps.client.tools).toHaveBeenCalledTimes(1);
    expect(deps.client.agentStream).not.toHaveBeenCalled();
    expect(screen.getByText('agent_ready · YES')).toBeTruthy();
    MOBILE_AGENT_TOOLS.forEach((name) => expect(screen.getByText(name)).toBeTruthy());
    expect(screen.getByText(/Articles stay outside this run/)).toBeTruthy();
  });

  it('keeps Start disabled when capability validation is unavailable', async () => {
    const deps = dependencies();
    deps.client.tools.mockResolvedValue({ ...catalog, tools: catalog.tools.slice(1) });
    render(<ResearchRunScreen {...deps.props} />);

    expect(await screen.findByText(/Required tools are unavailable: analyze_ticker/)).toBeTruthy();
    expect(screen.getByText('agent_ready · YES')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Start AAPL Research Run' })).toBeDisabled();
    expect(deps.client.agentStream).not.toHaveBeenCalled();
  });

  it('starts only after an explicit press, batches text near 40ms, completes, and saves terminal output', async () => {
    const deps = dependencies();
    const run = deferred<ReturnType<typeof completedState>>();
    let onEvent: EventHandler = () => undefined;
    const cancel = jest.fn();
    deps.client.agentStream.mockImplementation((_request, options) => {
      onEvent = options.onEvent;
      return { result: run.promise, cancel, controller: new AbortController() };
    });
    render(<ResearchRunScreen {...deps.props} />);
    await screen.findByText('Research access is ready.');

    fireEvent.press(screen.getByRole('button', { name: 'Start AAPL Research Run' }));
    expect(deps.client.agentStream).toHaveBeenCalledWith({
      messages: [{ role: 'user', content: RESEARCH_MESSAGE }],
      context: 'Ticker: AAPL\nPeriod: 1y',
    }, expect.objectContaining({ onEvent: expect.any(Function) }));

    act(() => {
      onEvent({ type: 'start', model: 'claude-sonnet', tools: [...MOBILE_AGENT_TOOLS] });
      onEvent({ type: 'text', text: 'Batched signal text' });
    });
    expect(screen.queryByText('Batched signal text')).toBeNull();
    await act(async () => new Promise((resolve) => setTimeout(resolve, 45)));
    expect(screen.getByText('Batched signal text')).toBeTruthy();

    await act(async () => run.resolve(completedState()));
    expect(await screen.findByText('Completed AAPL research')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Start AAPL Research Run' })).toBeNull();
    expect(screen.queryByText('Run completed')).toBeNull();
    const save = screen.getByRole('button', { name: 'Save AAPL research on this device' });
    expect(StyleSheet.flatten(save.props.style).minHeight).toBeGreaterThanOrEqual(44);
    expect(deps.library.save).not.toHaveBeenCalled();
    fireEvent.press(save);
    await waitFor(() => expect(deps.library.save).toHaveBeenCalledWith(expect.objectContaining({
      status: 'completed',
      symbol: 'AAPL',
      period: '1y',
      summary: 'Completed AAPL research',
      tools: [...MOBILE_AGENT_TOOLS],
    })));
    expect(await screen.findByText('Saved on this device.')).toBeTruthy();
    expect(deps.client.agentStream).toHaveBeenCalledTimes(1);
  });

  it('cancels by invalidating the generation first and ignores a late completion', async () => {
    const deps = dependencies();
    const run = deferred<ReturnType<typeof completedState>>();
    const cancel = jest.fn();
    deps.client.agentStream.mockReturnValue({ result: run.promise, cancel, controller: new AbortController() });
    render(<ResearchRunScreen {...deps.props} />);
    await screen.findByText('Research access is ready.');
    fireEvent.press(screen.getByRole('button', { name: 'Start AAPL Research Run' }));
    fireEvent.press(screen.getByRole('button', { name: 'Cancel Research Run' }));

    expect(cancel).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Research cancelled.')).toBeTruthy();
    await act(async () => run.resolve(completedState('Late result must be ignored')));
    expect(screen.queryByText('Late result must be ignored')).toBeNull();
    expect(screen.queryByRole('button', { name: /Save AAPL research/ })).toBeNull();
    expect(deps.library.save).not.toHaveBeenCalled();
  });

  it('keeps partial text after an error and waits for a manual Retry', async () => {
    const deps = dependencies();
    const first = deferred<ReturnType<typeof completedState>>();
    const second = deferred<ReturnType<typeof completedState>>();
    let firstEvent: EventHandler = () => undefined;
    deps.client.agentStream
      .mockImplementationOnce((_request, options) => {
        firstEvent = options.onEvent;
        return { result: first.promise, cancel: jest.fn(), controller: new AbortController() };
      })
      .mockReturnValueOnce({ result: second.promise, cancel: jest.fn(), controller: new AbortController() });
    render(<ResearchRunScreen {...deps.props} />);
    await screen.findByText('Research access is ready.');
    fireEvent.press(screen.getByRole('button', { name: 'Start AAPL Research Run' }));
    act(() => firstEvent({ type: 'text', text: 'Partial evidence' }));
    await act(async () => first.reject(new Error('Provider interrupted the stream.')));

    expect(await screen.findByText('Provider interrupted the stream.')).toBeTruthy();
    expect(screen.getByText('Partial evidence')).toBeTruthy();
    expect(deps.client.agentStream).toHaveBeenCalledTimes(1);
    fireEvent.press(screen.getByRole('button', { name: 'Retry AAPL Research Run' }));
    expect(deps.client.agentStream).toHaveBeenCalledTimes(2);
    await act(async () => second.resolve(completedState('Retry complete')));
    expect(await screen.findByText('Retry complete')).toBeTruthy();
  });

  it('accepts the verified non-streaming fallback as terminal completion without auto-retrying', async () => {
    const deps = dependencies();
    deps.client.agentStream.mockReturnValue({
      controller: new AbortController(),
      cancel: jest.fn(),
      result: Promise.resolve({
        transport: 'fallback',
        state: { status: 'streaming', text: '', model: null, tools: [], events: [], error: null },
        fallback: {
          ok: true,
          model: 'claude-sonnet',
          tools: [...MOBILE_AGENT_TOOLS],
          text: 'Fallback complete',
          stopReason: 'end_turn',
          toolCalls: [{ name: 'stock_fax', ok: true, durationMs: 20, error: null }],
          toolTrace: ['stock_fax'],
          articles: [],
          artifacts: [],
        },
      }),
    });
    render(<ResearchRunScreen {...deps.props} />);
    await screen.findByText('Research access is ready.');
    fireEvent.press(screen.getByRole('button', { name: 'Start AAPL Research Run' }));
    expect(await screen.findByText('Fallback complete')).toBeTruthy();
    expect(screen.getByText('Non-streaming fallback')).toBeTruthy();
    expect(deps.client.agentStream).toHaveBeenCalledTimes(1);
  });

  it('disables Start offline without checking capability or changing prior claims', async () => {
    const deps = dependencies();
    render(<ResearchRunScreen {...deps.props} reachability="offline" />);
    expect(screen.getByText('Offline · new research is unavailable.')).toBeTruthy();
    expect(deps.client.tools).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Start AAPL Research Run' })).toBeDisabled();
  });

  it('reopens a saved completed AAPL result without any network request', async () => {
    const deps = dependencies();
    deps.library.read.mockResolvedValue({
      schemaVersion: 1,
      id: 'run-1',
      status: 'completed',
      symbol: 'AAPL',
      period: '1y',
      summary: 'Saved AAPL thesis',
      model: 'claude-sonnet',
      tools: [...MOBILE_AGENT_TOOLS],
      toolTrace: [{ name: 'analyze_ticker', status: 'completed', durationMs: 24, error: null }],
      artifacts: [],
      source: { kind: 'research-agent', transport: 'stream' },
      generatedAt: 100,
      cachedAt: 200,
      accessedAt: 300,
    });
    render(<ResearchRunScreen {...deps.props} reachability="offline" recordId="run-1" />);

    expect(await screen.findByText('Saved AAPL thesis')).toBeTruthy();
    expect(screen.getByText('On this device')).toBeTruthy();
    expect(screen.getByText(/analyze_ticker · completed/)).toBeTruthy();
    expect(deps.client.tools).not.toHaveBeenCalled();
    expect(deps.client.agentStream).not.toHaveBeenCalled();
  });

  it('cancels active work on unmount and prevents queued text batches from updating', async () => {
    const deps = dependencies();
    const run = deferred<ReturnType<typeof completedState>>();
    let onEvent: EventHandler = () => undefined;
    const cancel = jest.fn();
    deps.client.agentStream.mockImplementation((_request, options) => {
      onEvent = options.onEvent;
      return { result: run.promise, cancel, controller: new AbortController() };
    });
    const view = render(<ResearchRunScreen {...deps.props} />);
    await screen.findByText('Research access is ready.');
    fireEvent.press(screen.getByRole('button', { name: 'Start AAPL Research Run' }));
    act(() => onEvent({ type: 'text', text: 'Queued after teardown' }));
    view.unmount();
    expect(cancel).toHaveBeenCalledTimes(1);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 45));
      run.resolve(completedState('Late unmounted result'));
    });
    expect(deps.library.save).not.toHaveBeenCalled();
  });

  it('restores mounted lifecycle state across StrictMode effect replay', async () => {
    const deps = dependencies();
    render(<StrictMode><ResearchRunScreen {...deps.props} /></StrictMode>);
    expect(await screen.findByText('Research access is ready.')).toBeTruthy();
  });
});
