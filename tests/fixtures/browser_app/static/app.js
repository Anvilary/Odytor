// A reduced stand-in for a "hardware fit" list: the dataset is fetched once; sorting re-renders from memory.
const list = document.getElementById('list');
const status = document.getElementById('status');
const sort = document.getElementById('sort');
const engine = document.getElementById('engine');
let cache = null;

function render(models) {
  const rows = [...models];
  rows.sort(sort.value === 'name' ? (a, b) => a.name.localeCompare(b.name) : (a, b) => b.score - a.score);
  const shown = engine.value === 'all' ? rows : rows.filter((m) => m.engine === engine.value);
  list.replaceChildren(...shown.map((m) => {
    const item = document.createElement('li');
    item.innerHTML = '<span></span><span class="score"></span>';
    item.children[0].textContent = m.name;
    item.children[1].textContent = String(m.score);
    return item;
  }));
  status.textContent = `${shown.length} models`;
}

async function fetchModels() {
  status.textContent = 'Loading';
  const response = await fetch(`/api/models?engine=${engine.value}`);
  cache = await response.json();
  render(cache);
}

sort.addEventListener('change', () => {
  if (cache) render(cache);
  else fetchModels();
});
engine.addEventListener('change', () => fetchModels());
document.getElementById('scan').addEventListener('click', () => fetchModels());
fetchModels();
