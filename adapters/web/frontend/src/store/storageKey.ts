// localStorage 按实例分家：多开时几个号共用一个域名，也就共用同一份 localStorage。
import { MOUNT } from '../api/supervisorClient';

/**
 * 给 key 打上实例烙印。单实例时 MOUNT 为空，key 逐字不变，老数据照常读得到。
 *
 * 不分家的话切一次号就会互相覆盖：token 尤其致命 —— 各实例的 .admin_token 本来
 * 就不是同一个，登录校验失败时前端会顺手把存着的那个清掉，两边来回踢。
 */
export const scopedKey = (base: string): string => (MOUNT ? `${base}${MOUNT}` : base);
