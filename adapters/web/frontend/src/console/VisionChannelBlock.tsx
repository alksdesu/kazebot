// 看图渠道的回显。它写的是 data/config.yaml 的 system_models.image，
// 入口只留在渠道页的系统槽位一处，这里不再放第二套表单。
import { useEffect, useState } from 'react';

import { getSystemModels, type SystemModelSlot } from '../api/supervisorClient';
import { useSettingsStore } from '../store/settingsStore';
import { useViewStore } from '../store/viewStore';
import { Block, Empty, ErrorText, Facts, LinkButton, Panel, ReadOnlyRow } from './components';
import { SYSTEM_SLOTS_ANCHOR } from './SystemSlots';

const CHANNELS_TAB = 'providers';
const SLOT_HREF = `?view=settings&tab=${CHANNELS_TAB}#${SYSTEM_SLOTS_ANCHOR}`;

const say = (error: unknown): string => (error instanceof Error ? error.message : '出错了');

export const VisionChannelBlock = () => {
  const adminToken = useSettingsStore((state) => state.adminToken);
  const setSettingsTab = useViewStore((state) => state.setSettingsTab);
  const [slot, setSlot] = useState<SystemModelSlot | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!adminToken) return;
    void (async () => {
      try {
        const data = await getSystemModels(adminToken);
        setSlot(data.slots.find((item) => item.key === 'image') || null);
      } catch (caught) { setError(say(caught)); }
      setLoaded(true);
    })();
  }, [adminToken]);

  // 自己指了地址却没说是哪家，后端就按 OpenAI 格式发。
  const channel = slot?.provider
    || (slot?.base_url_raw ? 'openai（没选渠道，按 OpenAI 格式发）' : '跟随主渠道');

  // 先落 hash 再切页签：切换会 replaceState 重写地址，hash 原样带过去，渠道页据此滚到槽位。
  const openSlot = () => {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${SYSTEM_SLOTS_ANCHOR}`);
    setSettingsTab(CHANNELS_TAB);
  };

  return (
    <Block hint="打标和 read_image 工具共用这一条。它在渠道页的系统槽位里改，这里只显示当前值" title="看图渠道">
      {!loaded ? <Empty>正在读取看图渠道…</Empty> : (
        <Panel>
          {error && <ErrorText>{error}</ErrorText>}
          <ReadOnlyRow name="渠道" value={channel} />
          <ReadOnlyRow hint={slot?.base_url_raw && slot.base_url !== slot.base_url_raw ? `解析为 ${slot.base_url}` : undefined} name="地址" value={slot?.base_url_raw || '跟随主渠道'} />
          <ReadOnlyRow hint={slot?.model_raw && slot.model !== slot.model_raw ? `解析为 ${slot.model}` : undefined} name="模型" value={slot?.model_raw || '未设置'} />
          <ReadOnlyRow name="密钥" value={slot?.api_key_present ? slot.api_key_redacted : '跟随主渠道'} />
          <Facts>
            模型名不跟随主渠道 —— 主渠道那个多半正是看不了图的纯文本模型。没配模型时整库都打不上标。
          </Facts>
          <div className="mt-3">
            <LinkButton
              href={SLOT_HREF}
              onClick={(event) => { event.preventDefault(); openSlot(); }}
              tone="quiet"
            >
              去渠道页改读图槽位
            </LinkButton>
          </div>
        </Panel>
      )}
    </Block>
  );
};
