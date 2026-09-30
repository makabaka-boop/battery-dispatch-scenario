/* 虚拟电池情景页面（Vue 3，无构建步骤）。
 *
 * 关键一致性规则：
 *  1) 任何编辑都立即把表单标记为 dirty，并让已显示的求解计划“作废”（planStale），
 *     旧求解响应绝不作为新情景的计划；
 *  2) 保存必须携带保存时的修订号 expected_revision，服务端用它做乐观并发控制，
 *     冲突返回 409，页面拒绝覆盖并重新拉取；
 *  3) 求解同样携带修订号：情景在别处被改过则 409，要求按最新修订重新求解。
 */
const { createApp, reactive, computed } = Vue;

function defaultForm() {
  const T = 8;
  return {
    load: Array(T).fill(5),
    pv: Array(T).fill(2),
    price: Array(T).fill(3),
    capacity: 20,
    initial_soc: 5,
    final_min_soc: 5,
    max_charge: 5,
    max_discharge: 5,
  };
}

createApp({
  setup() {
    const state = reactive({
      record: { id: null, revision: 0 },
      form: defaultForm(),
      savedRevision: 0,        // 表单所依据的已保存修订号
      dirty: false,            // 表单是否有未保存编辑
      plan: null,              // 最近一次求解响应
      planRevision: null,      // 计划对应的修订号
      busy: false,
      message: { text: "", kind: "info" },
    });

    const rows = computed(() =>
      state.form.load.map((_, t) => ({
        load: state.form.load[t],
        pv: state.form.pv[t],
        price: state.form.price[t],
      }))
    );

    // 计划失效：情景被编辑，或计划修订号不等于当前已保存修订号
    const planStale = computed(
      () => state.plan !== null &&
            (state.dirty || state.planRevision !== state.savedRevision)
    );

    function flash(text, kind = "info") {
      state.message = { text, kind };
    }

    function markDirty() {
      state.dirty = true;
    }

    function planRow(t) {
      return state.plan && state.plan.evidence ? state.plan.evidence[t] : null;
    }

    function actionText(a) {
      if (a > 0) return `充 +${a}`;
      if (a < 0) return `放 ${a}`;
      return "静置 0";
    }

    async function api(path, options = {}) {
      const resp = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const detail = typeof data.detail === "object" && data.detail !== null
          ? data.detail.message : data.detail;
        const err = new Error(detail || `HTTP ${resp.status}`);
        err.status = resp.status;
        err.data = data.detail;
        throw err;
      }
      return data;
    }

    function adoptRecord(rec) {
      state.record.id = rec.id;
      state.record.revision = rec.revision;
      state.savedRevision = rec.revision;
      const s = rec.scenario;
      state.form = {
        load: [...s.load],
        pv: [...s.pv],
        price: [...s.price],
        capacity: s.capacity,
        initial_soc: s.initial_soc,
        final_min_soc: s.final_min_soc,
        max_charge: s.max_charge,
        max_discharge: s.max_discharge,
      };
      state.dirty = false;
    }

    function newScenario() {
      state.record = { id: null, revision: 0 };
      state.form = defaultForm();
      state.savedRevision = 0;
      state.dirty = false;
      state.plan = null;
      state.planRevision = null;
      flash("已切换到一份新的空白情景，填写后点击“创建并保存”。", "info");
    }

    async function reload() {
      if (!state.record.id) return;
      state.busy = true;
      try {
        const rec = await api(`/api/scenarios/${state.record.id}`);
        adoptRecord(rec);
        state.plan = null;
        state.planRevision = null;
        flash(`已重新加载修订号 ${rec.revision}，旧计划已清除，请重新求解。`, "info");
      } catch (e) {
        flash(e.message, "err");
      } finally {
        state.busy = false;
      }
    }

    async function save() {
      state.busy = true;
      try {
        const body = JSON.stringify({ ...state.form });
        let rec;
        if (state.record.id) {
          // 乐观并发：携带编辑所依据的修订号，冲突由服务端拒绝
          rec = await api(
            `/api/scenarios/${state.record.id}?expected_revision=${state.savedRevision}`,
            { method: "PUT", body }
          );
        } else {
          rec = await api("/api/scenarios", { method: "POST", body });
        }
        adoptRecord(rec);
        state.plan = null;       // 修订号前进，旧计划必然过期
        state.planRevision = null;
        flash(`已保存为修订号 ${rec.revision}，旧求解响应已作废，请重新求解。`, "info");
      } catch (e) {
        if (e.status === 409) {
          flash(
            `保存被拒绝（修订号竞争）：${e.message}。已为你重新加载最新情景，请核对后重试。`,
            "err"
          );
          await reload();
        } else {
          flash(e.message, "err");
        }
      } finally {
        state.busy = false;
      }
    }

    async function solve() {
      if (!state.record.id || state.dirty) return;
      state.busy = true;
      try {
        const data = await api(
          `/api/scenarios/${state.record.id}/solve?expected_revision=${state.savedRevision}`,
          { method: "POST" }
        );
        state.plan = data;
        state.planRevision = data.revision;
        if (data.feasible) {
          flash(`已得到修订号 ${data.revision} 的最优计划。`, "info");
        } else {
          flash(`修订号 ${data.revision} 无可行计划：${data.reason}`, "warn");
        }
      } catch (e) {
        if (e.status === 409) {
          flash(
            `求解被拒绝：该情景已被保存为更新的修订号（当前 rev ${e.data?.current_revision}），旧响应不会成为计划。请重新加载。`,
            "err"
          );
          await reload();
        } else {
          flash(e.message, "err");
        }
      } finally {
        state.busy = false;
      }
    }

    function resizePeriods(ev) {
      let t = parseInt(ev.target.value, 10);
      if (isNaN(t)) return;
      t = Math.max(8, Math.min(48, t));
      const cur = state.form.load.length;
      for (const key of ["load", "pv", "price"]) {
        if (t > cur) state.form[key] = state.form[key].concat(Array(t - cur).fill(0));
        else state.form[key] = state.form[key].slice(0, t);
      }
      markDirty();
    }

    return {
      rows, planStale, markDirty, planRow, actionText,
      newScenario, reload, save, solve, resizePeriods,
      record: state.record, form: state.form, dirty,
      savedRevision: computed(() => state.savedRevision),
      plan: computed(() => state.plan),
      planRevision: computed(() => state.planRevision),
      busy: computed(() => state.busy),
      message: computed(() => state.message),
    };
  },
}).mount("#app");
