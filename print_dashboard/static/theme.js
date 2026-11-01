// Runs before the page draws, so a saved theme doesn't flash the wrong colours.
try {
  const saved = localStorage.getItem("theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch {}
