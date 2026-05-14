async function compareAllWL() {
  var list = getWatchlist();
  if (list.length === 0) { alert("Нет ниш в работе!"); return; }
  var wlDiv = document.getElementById("watchlist");
  var cmpBlock = document.getElementById("wl-compare-block");
  if (!cmpBlock) {
    cmpBlock = document.createElement("div");
    cmpBlock.id = "wl-compare-block";
    cmpBlock.style.cssText = "margin-top:24px;";
    wlDiv.appendChild(cmpBlock);
  }
  var maxRevenue = Math.max.apply(null, list.map(function(n){ return n.revenue||0; }));
  var maxMargin = Math.max.apply(null, list.map(function(n){ return n.profit_pct||0; }));
  var maxBuyout = Math.max.apply(null, list.map(function(n){ return n.buyout_pct||0; }));
  var turnovers = list.filter(function(n){ return (n.turnover||0)>0; }).map(function(n){ return n.turnover; });
  var minTurnover = turnovers.length > 0 ? Math.min.apply(null, turnovers) : 0;
  var html = "";
  html += "<div style='background:#1a2035;border:1px solid #f59e0b33;border-radius:12px;padding:20px;margin-bottom:16px;'>";
  html += "<div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;'>";
  html += "<div style='font-size:15px;font-weight:600;color:#f59e0b;'>&#9878; Сравнение ниш</div>";
  html += "<div style='font-size:11px;color:#555;'>" + list.length + " ниш в работе</div>";
  html += "</div><div style='overflow-x:auto;'><table style='width:100%;border-collapse:collapse;font-size:11px;'>";
  html += "<thead><tr style='border-bottom:1px solid #2d3748;'>";
  html += "<th style='text-align:left;padding:8px 6px;color:#555;font-weight:500;'>НИША</th>";
  html += "<th style='text-align:right;padding:8px 6px;color:#555;font-weight:500;'>ВЫРУЧКА/МЕС</th>";
  html += "<th style='text-align:right;padding:8px 6px;color:#555;font-weight:500;'>ЦЕНА</th>";
  html += "<th style='text-align:right;padding:8px 6px;color:#555;font-weight:500;'>МАРЖА</th>";
  html += "<th style='text-align:right;padding:8px 6px;color:#555;font-weight:500;'>ВЫКУП</th>";
  html += "<th style='text-align:right;padding:8px 6px;color:#555;font-weight:500;'>ОБОРОТ</th>";
  html += "<th style='text-align:right;padding:8px 6px;color:#555;font-weight:500;'>SCORE</th>";
  html += "</tr></thead><tbody>";
  for (var i = 0; i < list.length; i++) {
    var n = list[i];
    var sn = n.name.indexOf(" / ") >= 0 ? n.name.split(" / ").slice(1).join(" / ") : n.name;
    var score = n.score || 0;
    var scoreColor = score >= 65 ? "#4ade80" : score >= 40 ? "#fbbf24" : "#ef4444";
    var rev = n.revenue || 0;
    var revColor = rev > 0 && rev === maxRevenue ? "#4ade80" : "#e2e8f0";
    var margin = Math.round((n.profit_pct||0)*100);
    var marginColor = (n.profit_pct||0) === maxMargin ? "#4ade80" : margin >= 20 ? "#e2e8f0" : "#ef4444";
    var buyout = Math.round((n.buyout_pct||0)*100);
    var buyoutColor = (n.buyout_pct||0) === maxBuyout ? "#4ade80" : "#e2e8f0";
    var turnover = Math.round(n.turnover||0);
    var turnoverColor = turnover > 0 && turnover === minTurnover ? "#4ade80" : turnover > 60 ? "#ef4444" : "#e2e8f0";
    html += "<tr style='border-bottom:1px solid #1e2433;'>";
    html += "<td style='padding:8px 6px;color:#e2e8f0;font-weight:500;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' title='" + sn + "'>" + sn + "</td>";
    html += "<td style='padding:8px 6px;text-align:right;color:" + revColor + ";'>" + fmt(rev) + "&#8381;</td>";
    html += "<td style='padding:8px 6px;text-align:right;color:#e2e8f0;'>" + Math.round(n.avg_price||0) + "&#8381;</td>";
    html += "<td style='padding:8px 6px;text-align:right;color:" + marginColor + ";'>" + margin + "%</td>";
    html += "<td style='padding:8px 6px;text-align:right;color:" + buyoutColor + ";'>" + buyout + "%</td>";
    html += "<td style='padding:8px 6px;text-align:right;color:" + turnoverColor + ";'>" + (turnover > 0 ? turnover + "д" : "-") + "</td>";
    html += "<td style='padding:8px 6px;text-align:right;color:" + scoreColor + ";font-weight:600;'>" + (score > 0 ? score : "-") + "</td>";
    html += "</tr>";
  }
  html += "</tbody></table></div>";
  html += "<div style='margin-top:8px;font-size:10px;color:#444;'>&#128994; Зелёный = лучший показатель</div></div>";
  html += "<div id='wl-compare-ai-block' style='background:#1a2035;border:1px solid #f59e0b33;border-radius:12px;padding:20px;'>";
  html += "<div style='font-size:14px;font-weight:600;color:#f59e0b;margin-bottom:12px;'>&#129302; AI Рекомендация</div>";
  html += "<div id='wl-compare-ai' style='color:#555;font-size:13px;'>Нажмите кнопку для AI анализа...</div>";
  html += "<button onclick='runCompareAI()' style='margin-top:12px;background:#f59e0b22;border:1px solid #f59e0b44;border-radius:8px;padding:8px 16px;color:#f59e0b;font-size:12px;font-weight:600;cursor:pointer;'>&#9654; Запустить AI анализ</button>";
  html += "</div>";
  cmpBlock.innerHTML = html;
  cmpBlock.scrollIntoView({behavior:"smooth"});
}

async function runCompareAI() {
  var list = getWatchlist();
  if (list.length === 0) return;
  var aiDiv = document.getElementById("wl-compare-ai");
  if (!aiDiv) return;
  var aiBlock = document.getElementById("wl-compare-ai-block");
  aiDiv.textContent = "Анализируем " + list.length + " ниш...";
  var nichesData = list.map(function(n) {
    return {
      name: n.name, display_name: n.display_name || n.name,
      revenue: n.revenue || 0, avg_price: n.avg_price || 0,
      profit_pct: n.profit_pct || 0, buyout_pct: n.buyout_pct || 0,
      turnover: n.turnover || 0, sellers: n.sellers || 0,
      sellers_with_sales: n.sellers_with_sales || 0, score: n.score || 0
    };
  });
  try {
    var resp = await fetch("/compare-stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({niches: nichesData})
    });
    if (!resp.ok) { aiDiv.textContent = "Ошибка сервера"; return; }
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buf = "";
    while (true) {
      var res = await reader.read();
      if (res.done) break;
      buf += decoder.decode(res.value, {stream: true});
      var parts = buf.split("\n\n");
      buf = parts.pop();
      for (var j = 0; j < parts.length; j++) {
        var line = parts[j].trim();
        if (!line.startsWith("data:")) continue;
        try {
          var d = JSON.parse(line.slice(5).trim());
          if (d.type === "progress") {
            aiDiv.textContent = "Генерирую анализ... " + d.chars + " символов";
          } else if (d.type === "done" && d.html) {
            aiDiv.innerHTML = d.html;
            var btn = aiBlock ? aiBlock.querySelector("button") : null;
            if (btn) btn.style.display = "none";
          }
        } catch(e) {}
      }
    }
  } catch(e) {
    aiDiv.textContent = "Ошибка: " + e.message;
  }
}
