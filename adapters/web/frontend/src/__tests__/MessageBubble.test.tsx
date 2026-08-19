// [2026-05-17] Tests for the rewritten chat-flow tool rendering.
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MessageBubble } from '../components/chat/MessageBubble';
import type { ChatMessage } from '../types';

describe('MessageBubble tool rendering', () => {
  it('renders finish as a task banner and keeps non-finish tools as expandable rows', () => {
    // The render contract follows Lim-Code's model: tool calls are first-class rows,
    // while finish text is displayed as message content instead of duplicated in tools.
    const message: ChatMessage = {
      id: 'assistant-1',
      conversationId: 'conv-1',
      role: 'assistant',
      content: '最终回复内容',
      createdAt: '2026-05-17T01:45:31.049966+00:00',
      toolCalls: [
        {
          id: 'jt_cmd',
          name: 'execute_command',
          summary: 'command: uname -s',
          arguments: { command: 'uname -s' },
          status: 'success',
          result: 'returncode=0 Linux',
        },
        {
          id: 'jt_finish',
          name: 'finish',
          summary: '',
          arguments: { text: '最终回复内容' },
          status: 'success',
          isAutoResult: true,
        },
      ],
    };

    render(<MessageBubble message={message} />);

    // [2026-05-17] The component renders completion as a subtle lowercase
    // divider, so the assertion follows the current UI contract instead of an
    // older title-case banner label.
    expect(screen.getByText(/任务完成/i)).toBeInTheDocument();
    expect(screen.getByText('execute_command')).toBeInTheDocument();
    expect(screen.queryByText('finish')).not.toBeInTheDocument();
    expect(screen.queryByText('returncode=0 Linux')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /execute_command command: uname -s/i }));

    expect(screen.getByText('returncode=0 Linux')).toBeInTheDocument();
  });

  it('shows rejected finish reason as an expandable error tool row', () => {
    // [2026-05-17] Rejected finish calls are intentionally not hidden as completion
    // controls. They remain ordinary expandable error rows so the user can inspect
    // why no accepted final completion happened.
    const message: ChatMessage = {
      id: 'assistant-2',
      conversationId: 'conv-1',
      role: 'assistant',
      content: '被拒绝的回复内容',
      createdAt: '2026-05-17T01:45:07.068396+00:00',
      toolCalls: [
        {
          id: 'jt_finish_bad',
          name: 'finish',
          summary: '',
          arguments: { text: '被拒绝的回复内容' },
          status: 'error',
          rejected: true,
          result: '❌ REJECTED: finish cannot be called alongside other tools.',
        },
      ],
    };

    render(<MessageBubble message={message} />);

    expect(screen.getByText('finish')).toBeInTheDocument();
    expect(screen.queryByText(/task rejected/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /finish/i }));

    expect(screen.getByText('错误')).toBeInTheDocument();
    expect(screen.getByText('❌ REJECTED: finish cannot be called alongside other tools.')).toBeInTheDocument();
  });
});
