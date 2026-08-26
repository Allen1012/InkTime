/**
 * 后台表格缩略图的悬停大图预览。
 *
 * 为什么不用纯 CSS 的 :hover 放大：
 * 表格外层是带 overflow 的 .table-wrap，绝对定位的浮层会被祖先裁切；把大图放在
 * 单元格内还会撑大行高，整张表跟着跳动。业界通行做法是 Bootstrap popover 的
 * container: 'body'——浮层脱离被裁切的容器，由脚本定位。
 *
 * 这里再往前一步用原生 Popover API：popover 元素进入 top layer，天然不受祖先
 * overflow 与 z-index 影响，关闭、Escape 与无障碍语义由浏览器接管。不支持时
 * 回退到 position: fixed 加 class 切换，效果一致，只是少了平台托管的那部分。
 *
 * 单例浮层而非每行一个：一页最多 100 行，每行塞一个隐藏大图会白白多出 100 个
 * DOM 节点，并在页面加载时触发同样多的图片请求。
 *
 * 大图直接复用缩略图地址：该图浏览器已经因为列表渲染而缓存，弹出是瞬时的、不产生
 * 额外流量。改用原图会在每次悬停时拉取数兆字节，代价与收益不成比例。
 */
(function () {
  'use strict';

  // 悬停延迟：鼠标扫过整列时不应该一路弹窗，与 Tippy 的 delay 同一用途
  var SHOW_DELAY_MS = 120;
  // 浮层与触发元素之间的间距，也用作视口边缘的最小留白
  var GAP = 12;

  var layer = null;
  var layerImage = null;
  var layerCaption = null;
  var showTimer = null;
  var activeTrigger = null;
  var usesPopover = false;

  /** 惰性创建单例浮层，挂在 body 下以脱离表格的裁切容器。 */
  function ensureLayer() {
    if (layer) return layer;
    layer = document.createElement('div');
    layer.className = 'thumb-preview-layer';
    layer.setAttribute('role', 'tooltip');
    // manual 表示由脚本控制开关：auto 会带上点击外部自动关闭的轻量关闭行为，
    // 与悬停触发叠在一起会互相打断。
    usesPopover = 'popover' in HTMLElement.prototype;
    if (usesPopover) layer.setAttribute('popover', 'manual');
    layerImage = document.createElement('img');
    layerImage.alt = '';
    layerCaption = document.createElement('span');
    layerCaption.className = 'thumb-preview-caption';
    layer.appendChild(layerImage);
    layer.appendChild(layerCaption);
    document.body.appendChild(layer);
    return layer;
  }

  /**
   * 把浮层定位在触发元素旁，并在空间不足时翻边、在越界时夹回视口。
   *
   * @param {Element} trigger 触发预览的缩略图容器
   */
  function position(trigger) {
    var anchor = trigger.getBoundingClientRect();
    var box = layer.getBoundingClientRect();
    var viewportWidth = document.documentElement.clientWidth;
    var viewportHeight = document.documentElement.clientHeight;

    // 默认放右侧；右侧放不下就翻到左侧，两侧都放不下时取留白更大的一侧
    var spaceRight = viewportWidth - anchor.right - GAP;
    var spaceLeft = anchor.left - GAP;
    var left;
    if (box.width <= spaceRight || spaceRight >= spaceLeft) {
      left = anchor.right + GAP;
    } else {
      left = anchor.left - GAP - box.width;
    }
    left = Math.max(GAP, Math.min(left, viewportWidth - box.width - GAP));

    // 垂直方向与触发元素居中对齐，再夹进视口
    var top = anchor.top + anchor.height / 2 - box.height / 2;
    top = Math.max(GAP, Math.min(top, viewportHeight - box.height - GAP));

    layer.style.left = Math.round(left) + 'px';
    layer.style.top = Math.round(top) + 'px';
  }

  /** 隐藏浮层并清理待触发的定时器。 */
  function hide() {
    if (showTimer) {
      clearTimeout(showTimer);
      showTimer = null;
    }
    activeTrigger = null;
    if (!layer) return;
    if (usesPopover && layer.matches(':popover-open')) layer.hidePopover();
    layer.classList.remove('is-visible');
  }

  /**
   * 展示指定触发元素对应的大图。
   *
   * 图片先加载再定位：未解码完成时浮层尺寸为零，此时算出的位置必然偏移。
   *
   * @param {Element} trigger 触发预览的缩略图容器
   */
  function show(trigger) {
    var source = trigger.getAttribute('data-preview-src');
    if (!source) return;
    ensureLayer();
    activeTrigger = trigger;
    layerCaption.textContent = trigger.getAttribute('data-preview-label') || '';

    function reveal() {
      if (activeTrigger !== trigger) return;
      if (usesPopover && !layer.matches(':popover-open')) layer.showPopover();
      layer.classList.add('is-visible');
      position(trigger);
    }

    if (layerImage.getAttribute('src') === source && layerImage.complete) {
      reveal();
      return;
    }
    layerImage.onload = reveal;
    // 缩略图端点可能因照片已隐藏或原文件缺失而 404，此时不弹空白框
    layerImage.onerror = function () {
      if (activeTrigger === trigger) hide();
    };
    layerImage.setAttribute('src', source);
  }

  /** 找到事件目标所属的预览触发元素。 */
  function triggerFrom(target) {
    if (!target || typeof target.closest !== 'function') return null;
    return target.closest('[data-preview-src]');
  }

  function handleEnter(event) {
    var trigger = triggerFrom(event.target);
    if (!trigger || trigger === activeTrigger) return;
    if (showTimer) clearTimeout(showTimer);
    showTimer = setTimeout(function () {
      showTimer = null;
      show(trigger);
    }, SHOW_DELAY_MS);
  }

  function handleLeave(event) {
    var trigger = triggerFrom(event.target);
    if (!trigger) return;
    // 移到浮层自身上不应关闭：浮层贴着触发元素，指针可能顺势划过去
    var next = event.relatedTarget;
    if (next && layer && layer.contains(next)) return;
    hide();
  }

  // 事件委托挂在 document 上：任务页的轮询脚本会重建行，逐元素绑定会随之失效
  document.addEventListener('mouseover', handleEnter);
  document.addEventListener('mouseout', handleLeave);
  // 键盘可达：缩略图容器多为链接，聚焦时同样给出预览
  document.addEventListener('focusin', function (event) {
    var trigger = triggerFrom(event.target);
    if (trigger) show(trigger);
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') hide();
  });
  // 滚动或改窗口后原位置已失效。重定位不如直接收起：悬停语义下指针已经离开原位。
  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) hide();
  });
})();
