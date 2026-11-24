(() => {
  let page = 0;
  const slides = () => [...document.querySelectorAll('#deck > section.slide[data-slide]')];
  const show = () => slides().forEach((slide, index) => { slide.hidden = index !== page; });
  const deck = {
    ready: new Promise(resolve => document.addEventListener('DOMContentLoaded', () => {
      document.documentElement.classList.add('deck-on');
      show();
      requestAnimationFrame(() => resolve());
    }, {once:true})),
    get count() { return slides().length; },
    get page() { return page; },
    go(next, step) {
      page = next;
      if (step === 'all') slides()[page].querySelectorAll('[data-waiting]').forEach(node => node.removeAttribute('data-waiting'));
      show();
    },
    setMode(mode) { document.documentElement.dataset.deckMode = mode; },
    audit(index) {
      const slide = slides()[index];
      return slide.hasAttribute('data-test-error') ? [{slide:index+1,level:'error',code:'test-error',message:'시험용 오류',target:'section.slide',rect:null}] : [];
    },
    serialize() {
      const root = document.documentElement.cloneNode(true);
      root.querySelectorAll('[data-deck-rt]').forEach(node => node.remove());
      root.querySelectorAll('[hidden]').forEach(node => node.removeAttribute('hidden'));
      root.classList.remove('deck-on');
      root.removeAttribute('data-deck-mode');
      return '<!doctype html>\n' + root.outerHTML;
    }
  };
  window.deck = deck;
})();
