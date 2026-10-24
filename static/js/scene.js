/* QuerySure presentation layer: landing-page interactions only.
   Purely visual. It never talks to the API and never touches auth or workspace state; app.js owns all behaviour.
   The page background is the single supplied photograph (static/assets/bg.jpeg), painted by CSS. This file draws nothing on
   top of it: the previous canvas "aurora" (synthetic stars, light pillars, animated bar columns) was removed because it
   overlapped the hero copy and CTAs and duplicated the photograph. Everything here degrades to the static markup in
   index.html when scripts or motion are unavailable. */
(function () {
  'use strict';

  var reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var $ = function (id) { return document.getElementById(id); };
  var clamp01 = function (v) { return v < 0 ? 0 : v > 1 ? 1 : v; };

  function initLanding() {
    initNav();
    initStages();
    initReveal();
    initPath();
  }

  function initNav() {
    var nav = document.querySelector('#screen-landing .nav-container');
    if (!nav) return;
    var on = function () { nav.classList.toggle('is-scrolled', window.scrollY > 24); };
    window.addEventListener('scroll', on, { passive: true });
    on();
  }

  /* "How it works": six stages, each showing the artifact that stage leaves behind. */
  function initStages() {
    var root = $('stageExplorer');
    if (!root) return;
    var tabs = Array.prototype.slice.call(root.querySelectorAll('[role=tab]'));
    var panels = Array.prototype.slice.call(root.querySelectorAll('[role=tabpanel]'));
    if (!tabs.length) return;
    var idx = 0, auto = !reduceMotion, timer = 0, inView = false;
    function select(i, user) {
      idx = (i + tabs.length) % tabs.length;
      tabs.forEach(function (t, k) {
        var on = k === idx;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        t.closest('li').classList.toggle('is-current', on);
        t.closest('li').classList.toggle('is-past', k < idx);
      });
      panels.forEach(function (p, k) { p.hidden = k !== idx; });
      root.style.setProperty('--stage', String(idx));
      if (user) { auto = false; clearInterval(timer); }
    }
    function loop() {
      clearInterval(timer);
      if (!auto || !inView) return;
      timer = setInterval(function () { select(idx + 1, false); }, 3600);
    }
    tabs.forEach(function (t, k) {
      t.addEventListener('click', function () { select(k, true); });
      t.addEventListener('keydown', function (e) {
        var n = null;
        if (e.key === 'ArrowDown' || e.key === 'ArrowRight') n = idx + 1;
        else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') n = idx - 1;
        else if (e.key === 'Home') n = 0;
        else if (e.key === 'End') n = tabs.length - 1;
        if (n !== null) { e.preventDefault(); select(n, true); tabs[idx].focus(); }
      });
    });
    root.addEventListener('pointerenter', function () { clearInterval(timer); });
    root.addEventListener('pointerleave', loop);
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) { inView = es[0].isIntersecting; loop(); }, { threshold: 0.35 }).observe(root);
    }
    select(0, false);
  }

  function initReveal() {
    var els = Array.prototype.slice.call(document.querySelectorAll('#screen-landing [data-reveal]'));
    if (!els.length) return;
    if (reduceMotion || !('IntersectionObserver' in window)) { els.forEach(function (e) { e.classList.add('is-in'); }); return; }
    document.documentElement.classList.add('has-reveal');
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('is-in'); io.unobserve(e.target); } });
    }, { rootMargin: '0px 0px -12% 0px', threshold: 0.08 });
    els.forEach(function (e) { io.observe(e); });
  }

  /* A single line of light that fills as you read down the page; sections hang their markers on it. */
  function initPath() {
    var story = $('story');
    if (!story) return;
    var ticking = false;
    function update() {
      ticking = false;
      var r = story.getBoundingClientRect();
      var vh = window.innerHeight || 800;
      var p = clamp01((vh * 0.62 - r.top) / Math.max(1, r.height));
      story.style.setProperty('--p', p.toFixed(4));
      var marks = story.querySelectorAll('[data-mark]');
      var line = vh * 0.62;
      for (var i = 0; i < marks.length; i++) {
        marks[i].classList.toggle('is-lit', marks[i].getBoundingClientRect().top < line);
      }
    }
    function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(update); } }
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    update();
  }

  function boot() { initLanding(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
