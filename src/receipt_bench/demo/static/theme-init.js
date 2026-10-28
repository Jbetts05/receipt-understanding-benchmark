(() => {
  const param = new URLSearchParams(window.location.search).get("scoutTheme");
  const theme = param === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", theme);
})();
