// 带令牌加载的图片：先取回文件再显示，令牌不进图片地址。lazy 时进入视口才取。
import { useEffect, useState, type ImgHTMLAttributes, type ReactNode } from 'react';

import { useAuthorizedObjectUrl, type AuthorizedObjectUrl } from '../../hooks/useAuthorizedObjectUrl';

interface AuthorizedImageProps extends Omit<ImgHTMLAttributes<HTMLImageElement>, 'src' | 'children'> {
  source: string;
  lazy?: boolean;
  /** 还在取的时候显示的内容；不给就先放一张没有地址的图占住版面。 */
  placeholder?: ReactNode;
  /** 取失败时显示的内容；不给同上。 */
  fallback?: (error: unknown) => ReactNode;
  /** 图片就绪后把地址和类型交给调用方，比如做「新标签页查看」。 */
  children?: (image: ReactNode, state: AuthorizedObjectUrl) => ReactNode;
}

function useNearViewport(lazy: boolean): [(element: HTMLElement | null) => void, boolean] {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const [visible, setVisible] = useState(!lazy || typeof IntersectionObserver === 'undefined');
  useEffect(() => {
    if (visible || !element) return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setVisible(true);
    }, { rootMargin: '200px' });
    observer.observe(element);
    return () => observer.disconnect();
  }, [element, visible]);
  return [setElement, visible];
}

export function AuthorizedImage({ source, lazy = false, placeholder, fallback, children, alt = '', ...imageProps }: AuthorizedImageProps) {
  const [observe, visible] = useNearViewport(lazy);
  const state = useAuthorizedObjectUrl(source, visible);

  if (state.error && fallback) return <>{fallback(state.error)}</>;
  if (!state.url) {
    if (visible && placeholder && !state.error) return <>{placeholder}</>;
    return <img {...imageProps} alt={alt} aria-busy={state.loading || undefined} ref={observe} />;
  }
  const image = <img {...imageProps} alt={alt} src={state.url} />;
  return <>{children ? children(image, state) : image}</>;
}
