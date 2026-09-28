// 页面上那一句提示：操作的结果和读取失败的原因分开记，合成一句显示。
import { useCallback, useState } from 'react';

import { appendSentence } from '../utils/sentence';

export interface PageNotice {
  /** 合成之后显示的那一句。 */
  notice: string;
  /** 操作的结果（保存、新建、删除的成败），留到下一次操作或手动刷新。 */
  setMessage: (text: string) => void;
  /** 读取的结果，读成功传空串。后台重读、定时轮询读成功时只清掉它，刚写下的操作结果不受影响；
   *  读失败的原因也只挂到下一次读成功，不会一直留着。页面上有几路各读各的数据时用 source 分开记，
   *  免得一路读成功把另一路的失败原因抹掉。 */
  setLoadError: (text: string, source?: string) => void;
  /** 手动刷新：两样都清。 */
  clearNotice: () => void;
}

export function usePageNotice(): PageNotice {
  const [message, setMessage] = useState('');
  const [loadErrors, setLoadErrors] = useState<Readonly<Record<string, string>>>({});
  // 同值不换对象：轮询每一轮读成功都会传一次空串，换了就平白重渲染一遍。
  const setLoadError = useCallback((text: string, source = '') => {
    setLoadErrors((current) => ((current[source] ?? '') === text ? current : { ...current, [source]: text }));
  }, []);
  const clearNotice = useCallback(() => {
    setMessage('');
    setLoadErrors((current) => (Object.keys(current).length ? {} : current));
  }, []);
  return { notice: Object.values(loadErrors).reduce(appendSentence, message), setMessage, setLoadError, clearNotice };
}
