export function activate(sdk) {
  const { createElement: h, Fragment, useEffect, useState } = sdk.React;
  const workspaceId = 'system-workspace';
  const label = String(sdk.config.workspace_label || '插件工作区');

  function Workspace() {
    const conversations = sdk.state.chat.use((state) => state.conversations);
    const [draft, setDraft] = useState('');
    const [notice, setNotice] = useState('');
    const [backend, setBackend] = useState('正在读取后端状态…');
    sdk.forms.useUnsavedChanges(Boolean(draft));
    useEffect(() => {
      let current = true;
      sdk.http.request('/plugins/system-workspace/status').then(async (response) => {
        if (!response.ok) throw new Error(`状态请求失败：${response.status}`);
        const state = await response.json();
        if (current) setBackend(`${state.process} · 心跳 ${state.ticks} · 最近事件 ${state.last_event || '无'}`);
      }).catch((error) => { if (current) setBackend(error.message); });
      return () => { current = false; };
    }, []);
    const send = async () => {
      try {
        await sdk.commands.execute('chat.send', draft);
        setDraft('');
        setNotice('消息已提交，返回聊天查看回复。');
      } catch (error) { setNotice(error.message); }
    };
    return h('section', { className: 'system-workspace-example' },
      h('h1', null, label),
      h('p', null, `当前有 ${conversations.length} 个会话。此页面直接使用宿主 React、状态和发送命令。`),
      h('p', { role: 'status' }, backend),
      h('label', null, '发送到当前会话', h('textarea', { value: draft, onChange: (event) => setDraft(event.target.value), className: 'app-input' })),
      h('button', { type: 'button', disabled: !draft.trim(), onClick: send, className: 'app-button' }, '发送'),
      notice && h('p', { role: 'status' }, notice),
      h('button', { type: 'button', onClick: () => sdk.commands.execute('navigation.workspace', 'chat') }, '返回聊天'));
  }
  function Sidebar() {
    return h('nav', { className: 'system-workspace-example', 'aria-label': '插件工作区导航' },
      h('button', { onClick: () => sdk.commands.execute('navigation.workspace', 'chat') }, '聊天'),
      h('button', { onClick: () => sdk.commands.execute('navigation.settings', 'plugins') }, '管理插件'));
  }
  sdk.views.register(workspaceId, {
    id: workspaceId, sidebar: () => h(Sidebar),
    header: () => h('header', { className: 'app-header' }, label), main: () => h(Workspace),
    hasRightPanel: () => false,
  });
  sdk.workspaces.register(workspaceId, { id: workspaceId, label, icon: 'extension', order: 30 });
  sdk.settings.register(workspaceId, { id: workspaceId, label, icon: 'extension', group: 'basics', order: 3, Page: Workspace });
  sdk.slots.wrap('chat.header', (next) => (context) => h(Fragment, null,
    next(context), h('div', { className: 'system-workspace-banner' },
      h('span', null, '系统插件已接入聊天'),
      h('button', { onClick: () => sdk.commands.execute('navigation.workspace', workspaceId) }, label))));
  sdk.settings.wrap('client', (next) => ({ ...next, Page: function ClientWithPlugin() {
    return h(Fragment, null, h('p', { className: 'system-workspace-banner' }, '此设置页由系统插件包装。'), h(next.Page));
  } }));
  sdk.commands.wrap('chat.send', (next) => async (text, attachments) => {
    const prefix = String(sdk.config.message_prefix || '');
    return next(prefix + text, attachments);
  });
}
