import { defineStore } from "pinia";
interface Message {
  role: "user" | "assistant";
  text: string;
  image?: string;
  error?: boolean;
}
interface Conversation {
  entries: Message[];
  draft: string;
  pending: boolean;
}
export const useChatStore = defineStore("console-chat", {
  state: () => ({
    epoch: 0,
    conversations: {} as Record<string, Conversation>
  }),
  actions: {
    conversation(id: string) {
      return (
        this.conversations[id] ??
        (this.conversations[id] = { entries: [], draft: "", pending: false })
      );
    },
    append(id: string, message: Message) {
      const c = this.conversation(id);
      c.entries.push(message);
      if (c.entries.length > 100) c.entries.splice(0, c.entries.length - 100);
    },
    forget(id: string): void {
      delete this.conversations[id];
    },
    clear() {
      this.epoch++;
      this.conversations = {};
    }
  }
});
