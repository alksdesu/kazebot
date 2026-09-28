// 页面上那一句提示：操作的结果和读取失败的原因分开记，合成一句显示。
import { useCallback, useState } from 'react';

import { appendSentence } from '../utils/sentence';

export interface PageNotice {
  /** 合成之后显示的那一句。 */
  notice: string;
  /** 操作的结果（保存、新建、删除的成败），留到下一次操作或手动刷新。 */
  setMessage: (text: string) => void;
  /** 读取的结果，读成功传空串。后台重读、定时轮询读成功时只清掉它，刚写下的操作结果不受影响；
   *  读失败的原因也只挂到下一次读成功，不会一直留着。 */
  setLoadError: (text: string) => void;
  /** 手动刷新：两样都清。 */
  clearNotice: () => void;
}

export function usePageNotice(): PageNotice {
  const [message, setMessage] = useState('');
  const [loadError, setLoadError] = useState('');
  const clearNotice = useCallback(() => {
    setMessage('');
    setLoadError('');
  }, []);
  return { notice: appendSentence(message, loadError), setMessage, setLoadError, clearNotice };
}
