<script setup lang="ts">
// inApp: what the header shows inside the Android app, which has its own navigation (nothing: hidden)
defineProps<{ inApp?: boolean }>();
</script>

<template>
  <header :class="{ bare: !inApp }">
    <div class="bar">
      <h1>Record Live</h1>
      <nav>
        <RouterLink to="/" exact-active-class="on">实时字幕</RouterLink>
        <RouterLink to="/conversations" :class="{ on: $route.path !== '/' }">对话</RouterLink>
      </nav>
      <slot />
    </div>
  </header>
</template>

<style scoped>
header {
  position: sticky;
  top: 0;
  background: var(--bg);
  border-bottom: 1px solid var(--line);
  z-index: 2;
}
.bar {
  max-width: 860px;
  margin: 0 auto;
  padding: 12px 16px;
  display: flex;
  align-items: baseline;
  gap: 16px;
}
h1 {
  font-size: 17px;
  margin: 0;
  color: var(--accent);
  white-space: nowrap;
}
nav {
  display: flex;
  gap: 16px;
  font-size: 14px;
  white-space: nowrap;
}
nav a {
  color: var(--muted);
}
nav a.on {
  color: var(--accent);
  font-weight: 600;
}
:global(.in-app nav),
:global(.in-app h1) {
  display: none;
}
:global(.in-app header.bare) {
  display: none;
}
@media (max-width: 600px) {
  h1 {
    display: none;
  }
  nav {
    gap: 12px;
    font-size: 13px;
  }
  .bar {
    padding: 8px 12px;
  }
}
</style>
