import { beforeEach, it, expect } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useChatStore } from "../src/store/modules/chat";
beforeEach(() => setActivePinia(createPinia()));
it("不同平台不共享历史或草稿", () => {
  const chat = useChatStore();
  chat.append("A", { role: "user", text: "私有提问" });
  chat.conversation("A").draft = "未发送内容";
  expect(chat.conversation("B").entries).toEqual([]);
  expect(chat.conversation("B").draft).toBe("");
});
it("每个平台只保留最近100条历史", () => {
  const chat = useChatStore();
  for (let i = 0; i < 105; i++)
    chat.append("A", { role: "user", text: String(i) });
  expect(chat.conversation("A").entries).toHaveLength(100);
  expect(chat.conversation("A").entries[0].text).toBe("5");
});
it("退出会话清理全部平台历史并使旧请求的epoch失效", () => {
  const chat = useChatStore();
  const epoch = chat.epoch;
  chat.append("A", { role: "assistant", text: "旧回答" });
  chat.clear();
  expect(chat.epoch).not.toBe(epoch);
  expect(chat.conversation("A").entries).toEqual([]);
});
