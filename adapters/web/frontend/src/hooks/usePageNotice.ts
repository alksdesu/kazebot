// 页面上的交代：操作做成的结果单独显示，没做成的原因和各路读取失败的原因合成一句报错。
import { useCallback, useState } from 'react';

import { appendSentence } from '../utils/sentence';

export interface PageNotice {
  /** 操作做成的结果。 */
  notice: string;
  /** 操作没做成的原因和读取失败的原因合成的一句。 */
  error: string;
  /** 操作做成的结果（保存、新建、删除），留到下一次操作或手动刷新。传空串就是把上一次操作的结果清掉。
   *  只做成了一半时 failure 是没做成的那一半：做成的照常交代，没做成的单独报错。 */
  setMessage: (text: string, failure?: string) => void;
  /** 操作失败的原因，留法和 setMessage 一样：一次操作只有一个结果，后记的换掉先记的，不论成败。 */
  setError: (text: string) => void;
  /** 读取的结果，读成功传空串。后台重读、定时轮询读成功时只清掉它，刚写下的操作结果不受影响；
   *  读失败的原因也只挂到下一次读成功，不会一直留着。页面上有几路各读各的数据时用 source 分开记，
   *  免得一路读成功把另一路的失败原因抹掉。 */
  setLoadError: (text: string, source?: string) => void;
  /** 手动刷新：全清。 */
  clearNotice: () => void;
}

interface Outcome {
  readonly done: string;
  readonly failure: string;
}

const NO_OUTCOME: Outcome = { done: '', failure: '' };

// 同值不换对象：选中一行就会记一次空结果，换了就平白重渲染一遍。
const settle = (done: string, failure: string) => (current: Outcome): Outcome => {
  if (current.done === done && current.failure === failure) return current;
  return done || failure ? { done, failure } : NO_OUTCOME;
};

export function usePageNotice(): PageNotice {
  const [outcome, setOutcome] = useState<Outcome>(NO_OUTCOME);
  const [loadErrors, setLoadErrors] = useState<Readonly<Record<string, string>>>({});
  const setMessage = useCallback((text: string, failure = '') => setOutcome(settle(text, failure)), []);
  const setError = useCallback((text: string) => setOutcome(settle('', text)), []);
  // 同值不换对象：轮询每一轮读成功都会传一次空串，换了就平白重渲染一遍。
  const setLoadError = useCallback((text: string, source = '') => {
    setLoadErrors((current) => ((current[source] ?? '') === text ? current : { ...current, [source]: text }));
  }, []);
  const clearNotice = useCallback(() => {
    setOutcome(NO_OUTCOME);
    setLoadErrors((current) => (Object.keys(current).length ? {} : current));
  }, []);
  return {
    notice: outcome.done,
    error: Object.values(loadErrors).reduce(appendSentence, outcome.failure),
    setMessage,
    setError,
    setLoadError,
    clearNotice,
  };
}
