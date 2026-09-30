import { createApp } from "vue";
import App from "./App.vue";
import { router } from "./router";
import "./style.css";

// Android WebView: the app has its own navigation
if (/; wv\)/.test(navigator.userAgent)) document.documentElement.classList.add("in-app");
createApp(App).use(router).mount("#app");
