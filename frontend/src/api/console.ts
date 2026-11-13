import axios from "axios";
import { ElMessage } from "element-plus";
let csrf = "";
export const setCsrf = (value: string) => {
  csrf = value;
};
const client = axios.create({
  baseURL: "/console/api",
  timeout: 65000,
  withCredentials: true
});
client.interceptors.request.use(config => {
  if (config.method?.toLowerCase() !== "get")
    config.headers["X-CSRF-Token"] = csrf;
  return config;
});
export async function request<T>(
  path: string,
  method = "GET",
  data?: unknown,
  signal?: AbortSignal
): Promise<T> {
  try {
    const response = await client.request<{
      code: number;
      data: T;
      message: string;
    }>({ url: path, method, data, signal });
    return response.data.data;
  } catch (error) {
    if (axios.isCancel(error)) throw error;
    const status = axios.isAxiosError(error) ? error.response?.status : 0;
    const payload = axios.isAxiosError(error)
      ? (error.response?.data as { message?: string })
      : undefined;
    if (status === 401 && path !== "/login" && path !== "/session")
      window.dispatchEvent(new Event("session-expired"));
    const message =
      payload?.message ??
      (axios.isAxiosError(error) && error.code === "ECONNABORTED"
        ? "请求超时，请缩小范围后重试"
        : "无法完成请求，请检查连接或稍后重试");
    throw new Error(message);
  }
}
export async function perform(
  task: () => Promise<unknown>,
  success?: string
): Promise<void> {
  try {
    await task();
    if (success) ElMessage.success(success);
  } catch (error) {
    if (!axios.isCancel(error))
      ElMessage.error(error instanceof Error ? error.message : "操作失败");
  }
}
