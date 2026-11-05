/* Crown Tech Hub storefront: back-to-top button, rotating product-card photos, product photo gallery. */
(function () {
  'use strict';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- back to top ---------- */
  var toTop = document.getElementById('toTop');
  if (toTop) {
    var ticking = false;
    var check = function () { toTop.classList.toggle('show', window.scrollY > 600); ticking = false; };
    window.addEventListener('scroll', function () { if (!ticking) { ticking = true; window.requestAnimationFrame(check); } }, { passive: true });
    toTop.addEventListener('click', function () { window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' }); });
    check();
  }

  /* ---------- product cards: gently rotate through the product's photos ---------- */
  var cards = [].slice.call(document.querySelectorAll('.card-media[data-images]'));
  if (cards.length) {
    var visible = new Set();
    var io = 'IntersectionObserver' in window ? new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { visible.add(e.target); } else { visible.delete(e.target); } });
    }, { threshold: 0.4 }) : null;

    var show = function (el, i) {
      var img = el._img, next = el._list[i], pre = new Image();
      pre.onload = function () {
        img.classList.add('fading');
        setTimeout(function () {
          img.src = next; el._i = i;
          [].forEach.call(el._dots.children, function (d, k) { d.className = k === i ? 'on' : ''; });
          img.classList.remove('fading');
        }, 220);
      };
      pre.src = next;
    };

    cards.forEach(function (el) {
      var list;
      try { list = JSON.parse(el.getAttribute('data-images')); } catch (err) { return; }
      var img = el.querySelector('img');
      if (!list || list.length < 2 || !img) return;
      el._list = list; el._i = 0; el._img = img; el._hover = false;
      var dots = document.createElement('span');
      dots.className = 'dots'; dots.setAttribute('aria-hidden', 'true');
      list.forEach(function (_, k) { var d = document.createElement('i'); if (k === 0) d.className = 'on'; dots.appendChild(d); });
      el.appendChild(dots); el._dots = dots;
      if (io) { io.observe(el); } else { visible.add(el); }
      if (reduce) return;                       // people who prefer less motion still see the dots, but nothing moves by itself
      el.addEventListener('mouseenter', function () { el._hover = true; });
      el.addEventListener('mouseleave', function () { el._hover = false; });
      setTimeout(function run() {
        if (visible.has(el) && !el._hover && !document.hidden) { show(el, (el._i + 1) % el._list.length); }
        setTimeout(run, 3600);
      }, 1200 + Math.random() * 2400);          // each card starts at a different moment so they don't all flip together
    });
  }

  /* ---------- product page gallery ---------- */
  var gal = document.querySelector('.gallery');
  if (gal) {
    var imgs = gal.querySelectorAll('.gal-img'), thumbs = gal.querySelectorAll('.gal-thumb');
    var count = gal.querySelector('.gal-count'), stage = gal.querySelector('.gal-stage');
    var n = imgs.length, cur = 0, timer = null, hovering = false;
    var stop = function () { if (timer) { clearInterval(timer); timer = null; } };
    var go = function (i, byUser) {
      cur = (i + n) % n;
      [].forEach.call(imgs, function (im, k) { im.classList.toggle('on', k === cur); });
      [].forEach.call(thumbs, function (t, k) { t.classList.toggle('on', k === cur); t.setAttribute('aria-current', k === cur ? 'true' : 'false'); });
      if (count) count.textContent = (cur + 1) + ' / ' + n;
      if (byUser) stop();                      // once someone takes over, stop rotating by itself
    };
    if (n > 1) {
      var prev = gal.querySelector('.gal-nav.prev'), next = gal.querySelector('.gal-nav.next');
      if (prev) prev.addEventListener('click', function () { go(cur - 1, true); });
      if (next) next.addEventListener('click', function () { go(cur + 1, true); });
      [].forEach.call(thumbs, function (t, k) { t.addEventListener('click', function () { go(k, true); }); });
      stage.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowLeft') { go(cur - 1, true); e.preventDefault(); }
        if (e.key === 'ArrowRight') { go(cur + 1, true); e.preventDefault(); }
      });
      var x0 = null, y0 = null;
      stage.addEventListener('touchstart', function (e) { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; stop(); }, { passive: true });
      stage.addEventListener('touchend', function (e) {
        if (x0 === null) return;
        var dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
        if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) { go(cur + (dx < 0 ? 1 : -1), true); }
        x0 = null;
      }, { passive: true });
      stage.addEventListener('mouseenter', function () { hovering = true; });
      stage.addEventListener('mouseleave', function () { hovering = false; });
      if (!reduce) {
        timer = setInterval(function () { if (!hovering && !document.hidden) go(cur + 1, false); }, 4500);
      }
    }
  }
})();
