import { createRouter, createWebHistory } from "vue-router";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: () => import("./pages/Live.vue"), meta: { title: "Record Live" } },
    {
      path: "/conversations",
      component: () => import("./pages/Conversations.vue"),
      meta: { title: "对话" },
    },
    {
      path: "/c/:id",
      component: () => import("./pages/Conversation.vue"),
      props: true,
      meta: { title: "对话" },
    },
    {
      path: "/record",
      component: () => import("./pages/Record.vue"),
      meta: { title: "live-record 录音" },
    },
  ],
});
router.afterEach((to) => {
  document.title = (to.meta.title as string | undefined) ?? "Record Live";
});
