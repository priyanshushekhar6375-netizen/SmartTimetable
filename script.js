/**
 * TimeOptima - Smart Timetable & Resource Optimizer Frontend Controller
 */

function getApiBaseUrl() {
    if (window.API_BASE_URL) {
        return window.API_BASE_URL.replace(/\/+$/, "");
    }
    const host = (window.location.hostname || "").toLowerCase();
    const isLocalDevHost = !host || host === "localhost" || host === "127.0.0.1" || host === "0.0.0.0" || host === "::1" || host === "[::1]";

    // When running locally (via VS Code Live Server 5500, Vite 5173, file://, localhost:8000, 127.0.0.1:8000, etc.),
    // connect directly to http://127.0.0.1:8000/api
    if (window.location.protocol === "file:" || isLocalDevHost) {
        return "http://127.0.0.1:8000/api";
    }

    if (window.location.protocol.startsWith("http")) {
        return `${window.location.origin}/api`;
    }
    return "http://127.0.0.1:8000/api";
}

const API = getApiBaseUrl();

let currentTab = "teachers";
let lastGeneratedTimetable = null;
let lastGeneratedSectionTimetables = {};
let lastGeneratedSectionsInfo = [];
let lastGeneratedValidationReport = [];
let currentViewMode = "section"; // "section", "all", "teacher", "room"
let currentTargetFilter = "all";
let selectedBranch = "all";
let selectedYear = "all";
let selectedSection = "all";
let tabDataCache = {};
let activeTimeSlotObjects = [];
let timeSlots = [];
let timetableInvalidated = false;
let timetableInvalidationMessage = "";

let modalState = {
    mode: "add",
    tab: "teachers",
    originalId: null
};

function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function updateSolverStatusBadges(solverStatus = "OPTIMAL") {
    const badge = document.getElementById("badgeSolverStatus");
    if (badge) {
        const isOptimal = (String(solverStatus).toUpperCase() === "OPTIMAL");
        if (isOptimal) {
            badge.className = "status-badge badge-optimized";
            badge.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> AI Optimal`;
            badge.title = "Mathematically Optimal - Objective Maximized";
        } else {
            badge.className = "status-badge badge-feasible";
            badge.innerHTML = `<i class="fa-solid fa-clock-rotate-left"></i> CP-SAT Feasible`;
            badge.title = "Feasible Conflict-Free Schedule (Hard constraints satisfied)";
        }
    }

    const badgeCpsat = document.getElementById("badgeCpsatVerified");
    if (badgeCpsat) {
        const hasReport = Array.isArray(lastGeneratedValidationReport) && lastGeneratedValidationReport.length > 0;
        const allPassed = hasReport && lastGeneratedValidationReport.every(item => item.status === "PASSED");
        if (!hasReport || allPassed) {
            badgeCpsat.className = "status-badge badge-cpsat";
            badgeCpsat.style.cssText = "";
            badgeCpsat.innerHTML = `<i class="fa-solid fa-microchip"></i> CP-SAT Verified`;
            badgeCpsat.title = "Integer Programming Constraint Satisfaction Verified (0 Gaps & Complete Period Fulfillment)";
        } else {
            badgeCpsat.className = "status-badge badge-unverified";
            badgeCpsat.style.cssText = "background:#fee2e2;color:#991b1b;border:1px solid #f87171;";
            badgeCpsat.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> Period Mismatch`;
            badgeCpsat.title = "Required weekly periods not fully satisfied";
        }
    }
}

async function fetchTimeSlots() {
    try {
        const slots = await api("/timeslots");
        if (Array.isArray(slots) && slots.length > 0) {
            activeTimeSlotObjects = slots;
            timeSlots = slots.map(s => (typeof s === "object" && s !== null && s.time) ? s.time : s);
            return true;
        } else {
            activeTimeSlotObjects = [];
            timeSlots = [];
            return false;
        }
    } catch (e) {
        activeTimeSlotObjects = [];
        timeSlots = [];
        return false;
    }
}

function invalidateClientTimetable(reason = "Data changed — please regenerate the timetable.") {
    lastGeneratedTimetable = null;
    lastGeneratedSectionTimetables = {};
    lastGeneratedSectionsInfo = [];
    lastGeneratedValidationReport = [];
    localStorage.removeItem("saved_timetable");
    timetableInvalidated = true;
    timetableInvalidationMessage = reason;

    const activeBanner = document.getElementById("activeTimetableHeader");
    if (activeBanner) activeBanner.innerHTML = "";
    renderValidationReport(null);

    // Reset live summary strip indicators
    const elSlots = document.getElementById("ttSummarySlots");
    const elTheory = document.getElementById("ttSummaryTheory");
    const elLabs = document.getElementById("ttSummaryLabs");
    const elBreaks = document.getElementById("ttSummaryBreaks");
    const elConflicts = document.getElementById("ttSummaryConflicts");

    if (elSlots) elSlots.textContent = timeSlots && timeSlots.length > 0 ? timeSlots.length : "--";
    if (elTheory) elTheory.textContent = "--";
    if (elLabs) elLabs.textContent = "--";
    if (elBreaks) elBreaks.textContent = "--";
    if (elConflicts) {
        elConflicts.textContent = "Pending Generation";
        elConflicts.classList.remove("text-emerald");
    }

    // Render warning alert on Final Timetable page
    const alertBox = document.getElementById("timetableAlert");
    if (alertBox) {
        alertBox.innerHTML = `
            <div class="status warn" style="margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">
                <div>
                    <i class="fa-solid fa-triangle-exclamation" style="margin-right:8px;color:#d97706"></i>
                    <strong>${escapeHtml(reason)}</strong>
                    <span style="display:block;font-size:0.85rem;color:var(--muted);margin-top:2px;">Underlying institutional resources were updated. Previous timetable is no longer valid.</span>
                </div>
                <button class="primary" style="padding:6px 14px;font-size:0.85rem;" onclick="page('solver')">
                    <i class="fa-solid fa-rotate"></i> Regenerate Now
                </button>
            </div>
        `;
    }

    render(null);
}

// ==============================================================================
// Core API Communication Helper
// ==============================================================================
async function api(path, opts = {}) {
    let cleanPath = path.startsWith("/") ? path : `/${path}`;
    if (cleanPath.startsWith("/api/")) {
        cleanPath = cleanPath.slice(4);
    } else if (cleanPath === "/api") {
        cleanPath = "";
    }
    const url = API + cleanPath;
    const token = localStorage.getItem("authToken");
    const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    if (token) {
        headers["Authorization"] = `Bearer ${token}`;
    }
    const config = {
        ...opts,
        headers
    };

    let response;
    try {
        response = await fetch(url, config);
    } catch (networkErr) {
        throw new Error(`Failed to connect to backend at ${API}. Make sure FastAPI is running.`);
    }

    if (response.status === 401 && !cleanPath.includes("/login")) {
        localStorage.removeItem("authToken");
        localStorage.removeItem("isLoggedIn");
        const loginSection = document.getElementById("login");
        const appSection = document.getElementById("app");
        const loginMsg = document.getElementById("loginMsg");
        if (loginSection && appSection) {
            appSection.classList.add("hidden");
            loginSection.classList.remove("hidden");
            if (loginMsg) {
                loginMsg.className = "status warn";
                loginMsg.textContent = "Session expired or unauthorized. Please log in.";
            }
        }
        let errData = {};
        try { errData = await response.json(); } catch(e) {}
        throw new Error(errData.detail || "Session expired or unauthorized. Please log in.");
    }

    let data = {};
    try {
        data = await response.json();
    } catch (parseErr) {
        data = {};
    }

    if (!response.ok) {
        const message = data.detail || data.message || `HTTP ${response.status}: Request failed`;
        throw new Error(message);
    }

    return data;
}

// ==============================================================================
// Navigation & Views
// ==============================================================================
function page(id) {
    sessionStorage.setItem("currentPage", id);

    document.querySelectorAll(".page").forEach(el => el.classList.add("hidden"));
    const target = document.getElementById(id);
    if (target) {
        target.classList.remove("hidden");
    }

    document.querySelectorAll(".nav button").forEach(b => {
        b.classList.toggle("active", b.dataset.page === id);
    });

    const sidebar = document.getElementById("sideNav");
    if (sidebar && sidebar.classList.contains("open")) {
        sidebar.classList.remove("open");
    }

    if (id === "data") {
        const targetTab = currentTab || sessionStorage.getItem("currentTab") || "teachers";
        loadTab(targetTab);
    } else if (id === "dashboard") {
        refreshStats();
    } else if (id === "timetable") {
        applyTimetableFilter();
    }
}

function toggleSidebar() {
    const sidebar = document.getElementById("sideNav");
    if (sidebar) {
        sidebar.classList.toggle("open");
    }
}

// ==============================================================================
// Authentication & Session Management
// ==============================================================================
async function checkSession() {
    const token = localStorage.getItem("authToken");
    const isLoggedIn = localStorage.getItem("isLoggedIn") === "true";
    const loginSection = document.getElementById("login");
    const appSection = document.getElementById("app");

    if (isLoggedIn && token) {
        try {
            await api("/auth/verify");
            loginSection.classList.add("hidden");
            appSection.classList.remove("hidden");
            
            await fetchTimeSlots();
            
            // Fetch saved timetable from backend database
            try {
                const savedRes = await api("/timetable");
                if (savedRes && savedRes.status === "success" && Array.isArray(savedRes.timetable) && savedRes.timetable.length > 0) {
                    lastGeneratedTimetable = savedRes.timetable;
                    lastGeneratedSectionTimetables = savedRes.section_timetables || {};
                    lastGeneratedSectionsInfo = savedRes.sections_info || [];
                    lastGeneratedValidationReport = savedRes.validation_report || [];
                    timetableInvalidated = false;
                    timetableInvalidationMessage = "";
                    updateSolverStatusBadges(savedRes.solver_status || "OPTIMAL");
                    populateBranchYearSectionFilters();
                    onViewModeChange();
                } else {
                    lastGeneratedTimetable = null;
                    lastGeneratedSectionTimetables = {};
                    lastGeneratedSectionsInfo = [];
                    lastGeneratedValidationReport = [];
                    localStorage.removeItem("saved_timetable");
                    timetableInvalidated = true;
                    timetableInvalidationMessage = (savedRes && savedRes.message) || "Data changed — please regenerate the timetable.";
                }
            } catch (e) {
                lastGeneratedTimetable = null;
                lastGeneratedSectionTimetables = {};
                lastGeneratedSectionsInfo = [];
                lastGeneratedValidationReport = [];
                localStorage.removeItem("saved_timetable");
                timetableInvalidated = true;
                timetableInvalidationMessage = "Data changed — please regenerate the timetable.";
            }

            // Restore saved view (defaults to dashboard if no previous page)
            const savedPage = sessionStorage.getItem("currentPage") || "dashboard";
            page(savedPage);
            return;
        } catch (err) {
            console.warn("Session verification failed:", err);
            localStorage.removeItem("authToken");
            localStorage.removeItem("isLoggedIn");
        }
    }
    appSection.classList.add("hidden");
    loginSection.classList.remove("hidden");
}

document.getElementById("loginForm").onsubmit = async (e) => {
    e.preventDefault();
    const loginMsg = document.getElementById("loginMsg");
    const usernameInput = document.getElementById("username");
    const passwordInput = document.getElementById("password");

    loginMsg.className = "status";
    loginMsg.textContent = "Authenticating...";

    try {
        const data = await api("/login", {
            method: "POST",
            body: JSON.stringify({
                username: usernameInput.value.trim(),
                password: passwordInput.value.trim()
            })
        });

        const token = data.token || data.access_token;
        if (token) {
            localStorage.setItem("authToken", token);
        }
        localStorage.setItem("isLoggedIn", "true");
        localStorage.setItem("username", data.username || usernameInput.value.trim());
        loginMsg.textContent = "";
        loginMsg.className = "";
        
        document.getElementById("login").classList.add("hidden");
        document.getElementById("app").classList.remove("hidden");
        page("dashboard");
        await fetchTimeSlots();
        refreshStats();
    } catch (err) {
        loginMsg.className = "status error";
        loginMsg.textContent = err.message || "Invalid credentials.";
    }
};

document.getElementById("logout").onclick = async () => {
    try {
        await api("/logout", { method: "POST" });
    } catch (e) {
        // ignore
    }
    localStorage.removeItem("authToken");
    localStorage.removeItem("isLoggedIn");
    localStorage.removeItem("username");
    lastGeneratedTimetable = null;
    lastGeneratedSectionTimetables = {};
    lastGeneratedSectionsInfo = [];
    lastGeneratedValidationReport = [];
    document.getElementById("app").classList.add("hidden");
    document.getElementById("login").classList.remove("hidden");
};

// ==============================================================================
// Data Management Configs (Teachers, Rooms, Labs, Sections, Subjects, TimeSlots)
// ==============================================================================
const configs = {
    teachers: {
        title: "Teachers Management",
        singular: "Teacher",
        idKey: "name",
        endpoint: "teachers",
        fields: [
            ["name", "Teacher Name", "text", "e.g. Dr. Smith"],
            ["subject", "Primary Subject", "text", "e.g. Data Structures"],
            ["workload", "Classes / Week", "number", "2"],
            ["availability", "Available Days", "text", "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"]
        ]
    },
    rooms: {
        title: "Classrooms Management",
        singular: "Classroom",
        idKey: "id",
        endpoint: "rooms",
        fields: [
            ["id", "Room ID", "text", "e.g. Room 101"],
            ["capacity", "Student Capacity", "number", "60"],
            ["location", "Location / Block", "text", "Main Block"],
            ["availability", "Available Days", "text", "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"]
        ]
    },
    labs: {
        title: "Laboratories Management",
        singular: "Lab",
        idKey: "id",
        endpoint: "labs",
        fields: [
            ["id", "Lab ID", "text", "e.g. CS Lab 1"],
            ["capacity", "Capacity", "number", "60"],
            ["equipment", "Equipment / Specs", "text", "60 PCs, Linux OS"],
            ["availability", "Available Days", "text", "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"]
        ]
    },
    sections: {
        title: "Class Sections Management (Unlimited)",
        singular: "Section",
        idKey: "name",
        endpoint: "sections",
        fields: [
            ["name", "Class / Section Name", "text", "e.g. CSE-3A"],
            ["year", "Academic Year", "text", "e.g. 3rd Year"],
            ["branch", "Branch / Department", "text", "e.g. Computer Science"],
            ["section", "Section Code", "text", "e.g. A"],
            ["strength", "Student Strength", "number", "50"]
        ]
    },
    subjects: {
        title: "Subjects & Activity Allocation",
        singular: "Subject / Activity",
        idKey: "code",
        endpoint: "subjects",
        fields: [
            ["code", "Subject / Activity Code", "text", "e.g. DS or LIB101"],
            ["name", "Subject / Activity Name", "text", "e.g. Data Structures or Library"],
            ["teacher", "Assigned Teacher / In-Charge", "text", "e.g. Dr. Smith"],
            ["section", "Assigned Section", "text", "e.g. CSE-3A"],
            ["type", "Resource Type", "select", "Classroom", ["Classroom", "Lab", "Library"]],
            ["classes_per_week", "Periods / Week", "number", "2"],
            ["duration", "Duration (1 or 2 Periods)", "select", "1", ["1", "2"]],
            ["preferred_day", "Preferred Day (Optional)", "select", "Any", ["Any", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]],
            ["preferred_period", "Preferred Period (Optional)", "select", "Any", ["Any", "09:00 - 10:00", "10:00 - 11:00", "11:00 - 12:00", "13:00 - 14:00", "14:00 - 15:00", "15:00 - 16:00"]],
            ["availability", "Available Days (Optional)", "text", "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"],
            ["required_equipment", "Required Lab Equipment / Specs (Optional)", "text", "e.g. PC, Linux, Chipboard"]
        ]
    },
    timeslots: {
        title: "Time Slot Management",
        singular: "Time Slot",
        idKey: "time",
        endpoint: "timeslots",
        fields: [
            ["time", "Time Interval", "text", "HH:MM - HH:MM"],
            ["type", "Slot Type", "select", "Teaching", ["Teaching", "Break"]],
            ["order_index", "Display Sequence", "number", "1"]
        ]
    }
};

function renderFieldInput(prefix, f, defaultVal = "") {
    const fieldId = `${prefix}_${f[0]}`;
    const labelText = f[1];
    const type = f[2];
    const placeholder = f[3] || "";
    let options = f[4] || [];

    // Dynamically inject loaded time slots for preferred_period
    if (f[0] === "preferred_period" && timeSlots && timeSlots.length > 0) {
        options = ["Any", ...timeSlots];
    }

    const isOptional = (f[0] === "required_equipment" || f[0] === "preferred_period" || f[0] === "preferred_day" || f[0] === "availability");

    if (type === "select") {
        const optHtml = options.map(opt => {
            const selected = String(opt).toLowerCase() === String(defaultVal || placeholder).toLowerCase() ? 'selected' : '';
            return `<option value="${escapeHtml(opt)}" ${selected}>${escapeHtml(opt)}</option>`;
        }).join("");
        return `
            <div>
                <label for="${fieldId}">${labelText}</label>
                <select id="${fieldId}" style="width:100%" ${isOptional ? '' : 'required'}>
                    ${optHtml}
                </select>
            </div>
        `;
    }

    const valAttr = defaultVal !== "" ? `value="${escapeHtml(String(defaultVal))}"` : '';
    const numAttr = type === "number" ? `min="1" ${defaultVal === "" ? `value="${placeholder || '1'}"` : ''}` : '';

    return `
        <div>
            <label for="${fieldId}">${labelText}</label>
            <input id="${fieldId}" type="${type}" placeholder="${placeholder}" ${valAttr} ${numAttr} ${isOptional ? '' : 'required'}>
        </div>
    `;
}

async function loadTab(tab) {
    currentTab = tab;
    sessionStorage.setItem("currentTab", tab);

    // Synchronize tab buttons active class
    document.querySelectorAll("[data-tab]").forEach(b => {
        b.classList.toggle("active", b.dataset.tab === tab);
    });

    const c = configs[tab];
    const dataPanel = document.getElementById("dataPanel");
    if (!dataPanel) return;

    dataPanel.innerHTML = `<div class="card"><div class="spinner"></div><p style="text-align:center">Loading ${c.title}...</p></div>`;

    let items = [];
    try {
        items = await api("/" + c.endpoint);
        if (!Array.isArray(items)) items = [];
    } catch (e) {
        items = [];
    }
    tabDataCache[tab] = items;

    if (tab === "timeslots") {
        activeTimeSlotObjects = items;
        timeSlots = items.map(s => s.time);
    }

    const formInputs = c.fields.map(f => renderFieldInput("f", f)).join("");
    const tableHeaders = c.fields.map(f => `<th>${f[1]}</th>`).join("") + `<th style="text-align:center;width:170px">Actions</th>`;

    const tableRows = items.length > 0
        ? items.map(item => {
            const rawId = item[c.idKey];
            const encodedId = encodeURIComponent(String(rawId));
            const cells = c.fields.map(f => {
                const val = item[f[0]];
                if (tab === "timeslots" && f[0] === "type") {
                    const isBreak = String(val).toLowerCase() === "break";
                    return `<td><span class="badge" style="${isBreak ? 'background:#fef3c7;color:#92400e;border:1px solid #fde68a' : 'background:#dbeafe;color:#1e40af;border:1px solid #bfdbfe'}">${escapeHtml(String(val))}</span></td>`;
                }
                return `<td>${val !== undefined ? escapeHtml(String(val)) : ''}</td>`;
            }).join("");
            return `
                <tr>
                    ${cells}
                    <td style="text-align:center;white-space:nowrap">
                        <div class="actions-cell">
                            <button type="button" class="btn btn-edit btn-sm" onclick="openEditModal('${tab}', '${encodedId}')" title="Edit ${c.singular}">
                                ✏️ Edit
                            </button>
                            <button type="button" class="btn btn-delete btn-sm" onclick="deleteItem('${tab}', '${encodedId}')" title="Delete ${c.singular}">
                                🗑️ Delete
                            </button>
                        </div>
                    </td>
                </tr>
            `;
        }).join("")
        : `<tr><td colspan="${c.fields.length + 1}" style="text-align:center;color:var(--muted);padding:24px">No records found. Click <strong>➕ Add ${c.singular}</strong> above or use the form to add an entry.</td></tr>`;

    dataPanel.innerHTML = `
        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:14px">
                <div>
                    <h3 style="margin-bottom:2px">${c.title}</h3>
                    <p class="sub" style="margin-bottom:0">Configure and manage ${c.singular.toLowerCase()} records for constraint optimization.</p>
                </div>
                <button type="button" class="primary" onclick="openAddModal('${tab}')">
                    <i class="fa-solid fa-plus"></i> ➕ Add ${c.singular}
                </button>
            </div>
            
            <div id="dataTabMsg"></div>
            
            <form id="addResourceForm" onsubmit="event.preventDefault(); addItem('${tab}'); return false;">
                <div class="form-grid">
                    ${formInputs}
                    <button type="button" class="primary" onclick="addItem('${tab}')"><i class="fa-solid fa-plus"></i> ➕ Add ${c.singular}</button>
                </div>
            </form>
            
            <div class="table-wrap" style="margin-top: 24px">
                <table>
                    <thead><tr>${tableHeaders}</tr></thead>
                    <tbody>${tableRows}</tbody>
                </table>
            </div>
        </div>
    `;
    if (tab === "subjects") {
        setupSubjectWorkloadLiveIndicator("f");
    }
}

// ==============================================================================
// Teacher Workload Hard-Constraint Validation & Helpers
// ==============================================================================
async function validateTeacherWorkload(teacherName, addedPeriods, excludeSubjectCode = null) {
    const cleanTeacher = (teacherName || "").trim();
    if (!cleanTeacher) {
        return { valid: true };
    }

    // Ensure teachers cache is populated
    let teachers = tabDataCache["teachers"];
    if (!Array.isArray(teachers) || teachers.length === 0) {
        try {
            teachers = await api("/teachers");
            tabDataCache["teachers"] = teachers;
        } catch (e) {
            teachers = [];
        }
    }

    // Ensure subjects cache is populated
    let subjects = tabDataCache["subjects"];
    if (!Array.isArray(subjects) || subjects.length === 0) {
        try {
            subjects = await api("/subjects");
            tabDataCache["subjects"] = subjects;
        } catch (e) {
            subjects = [];
        }
    }

    const matchedTeacher = teachers.find(t => (t.name || "").trim().toLowerCase() === cleanTeacher.toLowerCase());
    if (!matchedTeacher) {
        return {
            valid: false,
            notFound: true,
            message: `Assigned teacher "${cleanTeacher}" does not exist in Teachers database.`
        };
    }

    const cap = Number(matchedTeacher.workload) || 0;
    const excludeClean = excludeSubjectCode ? String(excludeSubjectCode).trim().toLowerCase() : null;

    const assignedOther = subjects
        .filter(s => (s.teacher || "").trim().toLowerCase() === cleanTeacher.toLowerCase() &&
                     (!excludeClean || (s.code || "").trim().toLowerCase() !== excludeClean))
        .reduce((sum, s) => sum + (Number(s.classes_per_week) || 0), 0);

    const periodsNum = Number(addedPeriods) || 0;
    const newTotal = assignedOther + periodsNum;

    if (newTotal > cap) {
        const contextStr = excludeClean ? `assigned in other subjects: ${assignedOther}` : `currently assigned: ${assignedOther}`;
        return {
            valid: false,
            teacherName: matchedTeacher.name,
            workloadCap: cap,
            assignedTotal: newTotal,
            currentAssigned: assignedOther,
            requested: periodsNum,
            message: `Teacher "${matchedTeacher.name}" workload cap exceeded: Assigned total would be ${newTotal} classes/week (${contextStr}, requested: ${periodsNum}), which exceeds maximum capacity of ${cap} classes/week.`
        };
    }

    return {
        valid: true,
        teacherName: matchedTeacher.name,
        workloadCap: cap,
        assignedTotal: newTotal,
        currentAssigned: assignedOther,
        requested: periodsNum,
        remaining: cap - newTotal
    };
}

function setupSubjectWorkloadLiveIndicator(prefix, excludeCode = null) {
    const teacherInput = document.getElementById(`${prefix}_teacher`);
    const classesInput = document.getElementById(`${prefix}_classes_per_week`);
    if (!teacherInput || !classesInput) return;

    let hintEl = document.getElementById(`${prefix}_workload_hint`);
    if (!hintEl) {
        hintEl = document.createElement("div");
        hintEl.id = `${prefix}_workload_hint`;
        hintEl.style.cssText = "font-size:0.8rem;margin-top:6px;border-radius:6px;padding:6px 10px;display:none;transition:all 0.2s ease;";
        const parent = classesInput.closest("div") || classesInput.parentElement;
        if (parent) {
            parent.appendChild(hintEl);
        }
    }

    const updateHint = async () => {
        const teacherName = teacherInput.value.trim();
        const periods = Number(classesInput.value) || 0;
        if (!teacherName || periods <= 0) {
            hintEl.style.display = "none";
            return;
        }

        const res = await validateTeacherWorkload(teacherName, periods, excludeCode);
        if (res.notFound) {
            hintEl.style.display = "block";
            hintEl.className = "status warn";
            hintEl.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> Teacher "${escapeHtml(teacherName)}" not found in Teachers database.`;
        } else if (!res.valid) {
            hintEl.style.display = "block";
            hintEl.className = "status error";
            hintEl.innerHTML = `<i class="fa-solid fa-circle-xmark"></i> <strong>Workload Cap Exceeded:</strong> Total would be ${res.assignedTotal} / ${res.workloadCap} classes/week (+${res.requested} exceeds capacity by ${res.assignedTotal - res.workloadCap})`;
        } else {
            hintEl.style.display = "block";
            hintEl.className = "status success";
            hintEl.innerHTML = `<i class="fa-solid fa-circle-check"></i> <strong>Workload Capacity OK:</strong> Total will be ${res.assignedTotal} / ${res.workloadCap} classes/week (${res.remaining} remaining)`;
        }
    };

    teacherInput.addEventListener("input", updateHint);
    teacherInput.addEventListener("change", updateHint);
    classesInput.addEventListener("input", updateHint);
    classesInput.addEventListener("change", updateHint);
    if (excludeCode) {
        setTimeout(updateHint, 80);
    }
}

async function addItem(tab) {
    const c = configs[tab];
    const body = {};
    const msgEl = document.getElementById("dataTabMsg");

    for (const f of c.fields) {
        const input = document.getElementById("f_" + f[0]);
        const val = input ? input.value.trim() : "";
        if (!val) {
            const isOptional = (f[0] === "required_equipment" || f[0] === "preferred_period" || f[0] === "preferred_day" || f[0] === "availability");
            if (isOptional) {
                body[f[0]] = (f[0] === "preferred_day") ? "Any" : "";
                continue;
            }
            if (msgEl) {
                msgEl.className = "status error";
                msgEl.textContent = `Please fill out "${f[1]}".`;
            }
            return;
        }
        body[f[0]] = f[2] === "number" ? Number(val) : val;
    }

    if (tab === "subjects") {
        const dur = Number(body.duration) || 1;
        const periods = Number(body.classes_per_week) || 0;
        if (dur > 1 && periods < dur) {
            if (msgEl) {
                msgEl.className = "status error";
                msgEl.textContent = `A 2-period subject (Duration = 2) requires at least 2 Periods / Week.`;
            } else {
                alert(`A 2-period subject (Duration = 2) requires at least 2 Periods / Week.`);
            }
            return;
        }

        // Hard-constraint teacher workload capacity check before saving
        const workloadCheck = await validateTeacherWorkload(body.teacher, periods);
        if (!workloadCheck.valid) {
            if (msgEl) {
                msgEl.className = "status error";
                msgEl.textContent = workloadCheck.message;
            } else {
                alert(workloadCheck.message);
            }
            return;
        }
    }

    try {
        await api("/" + c.endpoint, {
            method: "POST",
            body: JSON.stringify(body)
        });
        
        tabDataCache["subjects"] = null;
        tabDataCache["teachers"] = null;
        currentTab = tab;
        await loadTab(tab);
        if (tab === "timeslots") {
            await fetchTimeSlots();
        }
        invalidateClientTimetable("Data changed — please regenerate the timetable.");
        showTabMessage(`✓ Successfully added ${c.singular} "${body[c.idKey]}".`, "success");
    } catch (err) {
        if (msgEl) {
            msgEl.className = "status error";
            msgEl.textContent = err.message || "Failed to add entry.";
        } else {
            alert(err.message);
        }
    }
}

function openAddModal(tab) {
    modalState = { mode: "add", tab: tab, originalId: null };
    const c = configs[tab];
    const modal = document.getElementById("crudModal");
    const modalTitle = document.getElementById("crudModalTitle");
    const modalFields = document.getElementById("modalFields");
    const submitBtn = document.getElementById("modalSubmitBtn");
    const msgEl = document.getElementById("modalMsg");

    if (msgEl) { msgEl.className = ""; msgEl.textContent = ""; }
    if (modalTitle) {
        modalTitle.innerHTML = `<i class="fa-solid fa-plus" style="color:var(--p)"></i> <span>➕ Add ${c.singular}</span>`;
    }
    if (submitBtn) {
        submitBtn.innerHTML = `<i class="fa-solid fa-plus"></i> ➕ Save ${c.singular}`;
        submitBtn.disabled = false;
    }

    if (modalFields) {
        modalFields.innerHTML = c.fields.map(f => renderFieldInput("m", f)).join("");
    }

    if (tab === "subjects") {
        setupSubjectWorkloadLiveIndicator("m", null);
    }

    modal.classList.add("active");
    setTimeout(() => {
        const firstInput = modalFields ? modalFields.querySelector("input, select") : null;
        if (firstInput) firstInput.focus();
    }, 50);
}

function openEditModal(tab, encodedId) {
    const rawId = decodeURIComponent(encodedId);
    modalState = { mode: "edit", tab: tab, originalId: rawId };
    const c = configs[tab];
    const modal = document.getElementById("crudModal");
    const modalTitle = document.getElementById("crudModalTitle");
    const modalFields = document.getElementById("modalFields");
    const submitBtn = document.getElementById("modalSubmitBtn");
    const msgEl = document.getElementById("modalMsg");

    if (msgEl) { msgEl.className = ""; msgEl.textContent = ""; }
    if (modalTitle) {
        modalTitle.innerHTML = `<i class="fa-solid fa-pen-to-square" style="color:var(--p)"></i> <span>✏️ Edit ${c.singular}</span>`;
    }
    if (submitBtn) {
        submitBtn.innerHTML = `<i class="fa-solid fa-check"></i> ✏️ Update ${c.singular}`;
        submitBtn.disabled = false;
    }

    const items = tabDataCache[tab] || [];
    const item = items.find(it => String(it[c.idKey]).trim().toLowerCase() === rawId.trim().toLowerCase());

    if (!item) {
        alert(`${c.singular} "${rawId}" not found in current list.`);
        return;
    }

    if (modalFields) {
        modalFields.innerHTML = c.fields.map(f => {
            const val = item[f[0]] !== undefined ? item[f[0]] : "";
            return renderFieldInput("m", f, val);
        }).join("");
    }

    if (tab === "subjects") {
        setupSubjectWorkloadLiveIndicator("m", rawId);
    }

    modal.classList.add("active");
    setTimeout(() => {
        const firstInput = modalFields ? modalFields.querySelector("input, select") : null;
        if (firstInput) firstInput.focus();
    }, 50);
}

function closeCrudModal() {
    const modal = document.getElementById("crudModal");
    if (modal) {
        modal.classList.remove("active");
    }
    const msgEl = document.getElementById("modalMsg");
    if (msgEl) { msgEl.className = ""; msgEl.textContent = ""; }
}

function handleModalOverlayClick(event) {
    if (event.target && event.target.id === "crudModal") {
        closeCrudModal();
    }
}

async function handleModalSubmit(event) {
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    const { mode, tab, originalId } = modalState;
    const c = configs[tab];
    const msgEl = document.getElementById("modalMsg");
    const submitBtn = document.getElementById("modalSubmitBtn");

    const body = {};
    for (const f of c.fields) {
        const input = document.getElementById("m_" + f[0]);
        const val = input ? input.value.trim() : "";
        if (!val) {
            if (f[0] === "required_equipment" || f[0] === "preferred_period") {
                body[f[0]] = "";
                continue;
            }
            if (f[0] === "availability" && tab === "subjects") {
                body[f[0]] = "";
                continue;
            }
            if (f[0] === "preferred_day") {
                body[f[0]] = "Any";
                continue;
            }
            if (f[0] === "duration") {
                body[f[0]] = 1;
                continue;
            }
            if (msgEl) {
                msgEl.className = "status error";
                msgEl.textContent = `Please enter "${f[1]}".`;
            }
            return;
        }

        // Validate availability if editing availability
        if (f[0] === "availability") {
            const allowed = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
            const tokens = val.replace(/;/g, ",").split(",").map(t => t.trim()).filter(Boolean);
            const invalid = tokens.filter(t => !allowed.includes(t.toLowerCase()));
            if (tokens.length > 0 && invalid.length > 0) {
                if (msgEl) {
                    msgEl.className = "status error";
                    msgEl.textContent = `Invalid day(s): "${invalid.join(", ")}". Allowed days: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday.`;
                }
                return;
            }
            if (tab !== "subjects" && tokens.length === 0) {
                if (msgEl) {
                    msgEl.className = "status error";
                    msgEl.textContent = `Availability must contain at least one valid day (Monday to Saturday).`;
                }
                return;
            }
        }

        body[f[0]] = f[2] === "number" ? Number(val) : val;
    }

    if (tab === "subjects") {
        const dur = Number(body.duration) || 1;
        const periods = Number(body.classes_per_week) || 0;
        if (dur > 1 && periods < dur) {
            if (msgEl) {
                msgEl.className = "status error";
                msgEl.textContent = `A 2-period subject (Duration = 2) requires at least 2 Periods / Week.`;
            } else {
                alert(`A 2-period subject (Duration = 2) requires at least 2 Periods / Week.`);
            }
            if (submitBtn) submitBtn.disabled = false;
            return;
        }

        // Hard-constraint teacher workload capacity check before saving modal
        const excludeCode = (mode === "edit") ? originalId : null;
        const workloadCheck = await validateTeacherWorkload(body.teacher, periods, excludeCode);
        if (!workloadCheck.valid) {
            if (msgEl) {
                msgEl.className = "status error";
                msgEl.textContent = workloadCheck.message;
            } else {
                alert(workloadCheck.message);
            }
            if (submitBtn) submitBtn.disabled = false;
            return;
        }
    }

    if (tab === "teachers" && mode === "edit") {
        const teacherName = originalId ? String(originalId).trim() : (body.name || "").trim();
        const newCap = Number(body.workload) || 0;
        let subjects = tabDataCache["subjects"];
        if (!Array.isArray(subjects) || subjects.length === 0) {
            try {
                subjects = await api("/subjects");
                tabDataCache["subjects"] = subjects;
            } catch (e) {
                subjects = [];
            }
        }
        const currentAssigned = subjects
            .filter(s => (s.teacher || "").trim().toLowerCase() === teacherName.toLowerCase())
            .reduce((sum, s) => sum + (Number(s.classes_per_week) || 0), 0);
        if (newCap < currentAssigned) {
            const errMsg = `Teacher "${body.name || teacherName}" workload cap cannot be reduced to ${newCap} classes/week: Currently assigned total is ${currentAssigned} classes/week across subjects. Maximum capacity must be at least ${currentAssigned} classes/week.`;
            if (msgEl) {
                msgEl.className = "status error";
                msgEl.textContent = errMsg;
            } else {
                alert(errMsg);
            }
            if (submitBtn) submitBtn.disabled = false;
            return;
        }
    }

    if (submitBtn) submitBtn.disabled = true;

    try {
        if (mode === "add") {
            await api("/" + c.endpoint, {
                method: "POST",
                body: JSON.stringify(body)
            });
        } else {
            await api(`/${c.endpoint}/${encodeURIComponent(originalId)}`, {
                method: "PUT",
                body: JSON.stringify(body)
            });
        }

        tabDataCache["subjects"] = null;
        tabDataCache["teachers"] = null;
        closeCrudModal();
        currentTab = tab;
        await loadTab(tab);
        if (tab === "timeslots") {
            await fetchTimeSlots();
        }
        invalidateClientTimetable("Data changed — please regenerate the timetable.");
        showTabMessage(
            mode === "add"
                ? `✓ Successfully added ${c.singular} "${body[c.idKey]}".`
                : `✓ Successfully updated ${c.singular} "${body[c.idKey]}".`,
            "success"
        );
    } catch (err) {
        if (msgEl) {
            msgEl.className = "status error";
            msgEl.textContent = err.message || "Operation failed.";
        } else {
            alert(err.message);
        }
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
}

async function deleteItem(tab, encodedId) {
    const rawId = decodeURIComponent(encodedId);
    const c = configs[tab];
    const confirmed = window.confirm(`Are you sure you want to delete ${c.singular} "${rawId}"?\n\nThis will remove it from the backend and database.`);
    if (!confirmed) return;

    try {
        await api(`/${c.endpoint}/${encodeURIComponent(rawId)}`, {
            method: "DELETE"
        });
        tabDataCache["subjects"] = null;
        tabDataCache["teachers"] = null;
        currentTab = tab;
        await loadTab(tab);
        if (tab === "timeslots") {
            await fetchTimeSlots();
        }
        invalidateClientTimetable("Data changed — please regenerate the timetable.");
        showTabMessage(`✓ Successfully deleted ${c.singular} "${rawId}".`, "success");
    } catch (err) {
        showTabMessage(err.message || `Failed to delete ${c.singular}.`, "error");
    }
}

function showTabMessage(msg, type = "success") {
    setTimeout(() => {
        const el = document.getElementById("dataTabMsg");
        if (el) {
            el.className = `status ${type}`;
            el.textContent = msg;
            if (type === "success") {
                setTimeout(() => {
                    if (el && el.textContent === msg) {
                        el.className = "";
                        el.textContent = "";
                    }
                }, 4000);
            }
        }
    }, 10);
}

async function refreshStats() {
    const statMappings = [
        ["sTeachers", "teachers"],
        ["sRooms", "rooms"],
        ["sLabs", "labs"],
        ["sSections", "sections"],
        ["sSubjects", "subjects"],
        ["sTimeslots", "timeslots"]
    ];

    for (const [elemId, endpoint] of statMappings) {
        const el = document.getElementById(elemId);
        if (!el) continue;
        try {
            const data = await api("/" + endpoint);
            el.textContent = Array.isArray(data) ? data.length : 0;
        } catch {
            el.textContent = "0";
        }
    }
}

// ==============================================================================
// Solver & Timetable Generation Integration
// ==============================================================================
document.querySelectorAll("[data-tab]").forEach(btn => {
    btn.onclick = () => {
        document.querySelectorAll("[data-tab]").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        loadTab(btn.dataset.tab);
    };
});

["morning", "gaps", "workload"].forEach(id => {
    const slider = document.getElementById(id);
    const label = document.getElementById(id + "Val");
    if (slider && label) {
        slider.oninput = () => {
            label.textContent = slider.value;
        };
    }
});

document.getElementById("generate").onclick = async () => {
    const loading = document.getElementById("loading");
    const generateBtn = document.getElementById("generate");
    const solverStatus = document.getElementById("solverStatus");
    const solverResult = document.getElementById("solverResult");

    loading.classList.remove("hidden");
    generateBtn.disabled = true;
    solverStatus.className = "status";
    solverStatus.textContent = "Solving CP-SAT constraints on FastAPI backend...";
    solverResult.innerHTML = "";

    const payload = {
        morning_preference: Number(document.getElementById("morning").value || 3),
        minimize_gaps: Number(document.getElementById("gaps").value || 2),
        balance_workload: Number(document.getElementById("workload").value || 2)
    };

    try {
        const data = await api("/generate-timetable", {
            method: "POST",
            body: JSON.stringify(payload)
        });

        if (data.status === "success" && Array.isArray(data.timetable)) {
            lastGeneratedTimetable = data.timetable;
            lastGeneratedSectionTimetables = data.section_timetables || {};
            lastGeneratedSectionsInfo = data.sections_info || [];
            lastGeneratedValidationReport = data.validation_report || [];
            timetableInvalidated = false;
            timetableInvalidationMessage = "";
            localStorage.setItem("saved_timetable", JSON.stringify(data.timetable));

            const alertBox = document.getElementById("timetableAlert");
            if (alertBox) {
                alertBox.innerHTML = "";
            }

            const isOptimal = (data.solver_status === "OPTIMAL");
            solverStatus.className = isOptimal ? "status success" : "status warn";
            solverStatus.textContent = isOptimal
                ? "✓ Mathematically Optimal conflict-free timetable generated and verified."
                : "✓ Feasible conflict-free timetable found within time limit (hard constraints verified).";

            updateSolverStatusBadges(data.solver_status || "OPTIMAL");

            const totalScheduled = data.timetable.reduce((acc, dayObj) => {
                return acc + dayObj.slots.reduce((sAcc, slot) => {
                    if (slot.sessions && slot.sessions.length > 0) {
                        return sAcc + slot.sessions.length;
                    }
                    return sAcc + (slot.slot_type === "Teaching" && slot.subject !== "Free" && slot.subject !== "Break/Free" ? 1 : 0);
                }, 0);
            }, 0);

            solverResult.innerHTML = `
                <div class="status ${isOptimal ? 'success' : 'warn'}">
                    <strong>${isOptimal ? 'CP-SAT Optimal:' : 'CP-SAT Feasible:'}</strong> ${data.message} (${totalScheduled} total classes scheduled across ${data.timetable.length} days).
                    <div style="margin-top: 10px;">
                        <button class="primary" onclick="page('timetable')">View Timetable ➔</button>
                    </div>
                </div>
            `;

            populateBranchYearSectionFilters();
            onViewModeChange();
            page("timetable");
        } else {
            lastGeneratedValidationReport = data.validation_report || [];
            updateSolverStatusBadges(data.solver_status || "INFEASIBLE");
            solverStatus.className = "status error";
            solverStatus.textContent = "Constraint Solver Conflict / Period Mismatch Detected.";
            solverResult.innerHTML = `
                <div class="status error">
                    <strong>${escapeHtml(data.message || "Constraint Satisfaction Failure")}:</strong>
                    <div style="margin-top:8px;white-space:pre-wrap;font-family:monospace;font-size:0.85rem;line-height:1.5;">${escapeHtml(data.conflicts || "No feasible solution found.")}</div>
                </div>
            `;
            showTimetableError(data.message, data.conflicts);
        }
    } catch (err) {
        solverStatus.className = "status error";
        solverStatus.textContent = "Backend Connection Error.";
        solverResult.innerHTML = `
            <div class="status error">
                <strong>Error:</strong> ${err.message}
            </div>
        `;
        showTimetableError("Backend Error", err.message);
    } finally {
        loading.classList.add("hidden");
        generateBtn.disabled = false;
    }
};

// ==============================================================================
// Multi-Section Cascading Filters (Branch -> Year -> Section + All Timetables)
// ==============================================================================
function getKnownSections() {
    if (Array.isArray(lastGeneratedSectionsInfo) && lastGeneratedSectionsInfo.length > 0) {
        return lastGeneratedSectionsInfo;
    }
    if (tabDataCache["sections"] && tabDataCache["sections"].length > 0) {
        return tabDataCache["sections"];
    }
    if (lastGeneratedSectionTimetables && Object.keys(lastGeneratedSectionTimetables).length > 0) {
        return Object.keys(lastGeneratedSectionTimetables).map(k => ({
            name: k,
            branch: "Computer Science",
            year: "3rd Year",
            section: k,
            strength: 50
        }));
    }
    return [];
}

function populateBranchYearSectionFilters() {
    const branchSelect = document.getElementById("filterBranchSelect");
    if (!branchSelect) return;

    const sections = getKnownSections();
    const branches = Array.from(new Set(sections.map(s => (s.branch || "General").trim()).filter(Boolean))).sort();

    if (branches.length === 0) {
        branchSelect.innerHTML = `<option value="">No Branches</option>`;
        return;
    }

    let html = "";
    branches.forEach(b => {
        html += `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`;
    });
    branchSelect.innerHTML = html;

    if (branches.includes(selectedBranch)) {
        branchSelect.value = selectedBranch;
    } else {
        selectedBranch = branches[0];
        branchSelect.value = selectedBranch;
    }

    onBranchFilterChange();
}

function onBranchFilterChange() {
    const branchSelect = document.getElementById("filterBranchSelect");
    const yearSelect = document.getElementById("filterYearSelect");
    if (!branchSelect || !yearSelect) return;

    selectedBranch = branchSelect.value;
    const sections = getKnownSections();

    const branchSections = sections.filter(s => (s.branch || "General").trim() === selectedBranch);
    const years = Array.from(new Set(branchSections.map(s => (s.year || "General").trim()).filter(Boolean))).sort();

    if (years.length === 0) {
        yearSelect.innerHTML = `<option value="">No Years</option>`;
        return;
    }

    let html = "";
    years.forEach(y => {
        html += `<option value="${escapeHtml(y)}">${escapeHtml(y)}</option>`;
    });
    yearSelect.innerHTML = html;

    if (years.includes(selectedYear)) {
        yearSelect.value = selectedYear;
    } else {
        selectedYear = years[0];
        yearSelect.value = selectedYear;
    }

    onYearFilterChange();
}

function onYearFilterChange() {
    const branchSelect = document.getElementById("filterBranchSelect");
    const yearSelect = document.getElementById("filterYearSelect");
    const sectionSelect = document.getElementById("filterSectionSelect");
    if (!branchSelect || !yearSelect || !sectionSelect) return;

    selectedYear = yearSelect.value;
    const sections = getKnownSections();

    let filtered = sections.filter(s => 
        (s.branch || "General").trim() === selectedBranch &&
        (s.year || "General").trim() === selectedYear
    );

    if (filtered.length === 0) {
        filtered = sections.filter(s => (s.branch || "General").trim() === selectedBranch);
    }
    if (filtered.length === 0) {
        filtered = sections;
    }

    let html = "";
    filtered.forEach(s => {
        const displayLabel = s.section ? `Section ${s.section} (${s.name})` : s.name;
        html += `<option value="${escapeHtml(s.name)}">${escapeHtml(displayLabel)}</option>`;
    });
    sectionSelect.innerHTML = html;

    const availableNames = filtered.map(s => s.name);
    if (availableNames.includes(selectedSection)) {
        sectionSelect.value = selectedSection;
    } else {
        selectedSection = availableNames.length > 0 ? availableNames[0] : "";
        sectionSelect.value = selectedSection;
    }

    onSectionFilterChange();
}

function onSectionFilterChange() {
    const sectionSelect = document.getElementById("filterSectionSelect");
    if (sectionSelect) {
        selectedSection = sectionSelect.value;
    }
    currentViewMode = "section";
    const modeSelect = document.getElementById("viewModeSelect");
    if (modeSelect) modeSelect.value = "section";
    const btnAll = document.getElementById("btnAllTimetables");
    if (btnAll) btnAll.classList.remove("active");
    applyTimetableFilter();
}

function showAllTimetables() {
    currentViewMode = "all";
    const modeSelect = document.getElementById("viewModeSelect");
    if (modeSelect) {
        modeSelect.value = "all";
    }
    const btnAll = document.getElementById("btnAllTimetables");
    if (btnAll) btnAll.classList.add("active");
    applyTimetableFilter();
}

function onViewModeChange() {
    const modeSelect = document.getElementById("viewModeSelect");
    const branchGroup = document.getElementById("filterBranchGroup");
    const yearGroup = document.getElementById("filterYearGroup");
    const sectionGroup = document.getElementById("filterSectionGroup");
    const targetGroup = document.getElementById("filterTargetGroup");
    const targetLabel = document.getElementById("filterTargetLabel");
    const targetSelect = document.getElementById("filterTargetSelect");
    const btnAll = document.getElementById("btnAllTimetables");

    if (!modeSelect) return;
    currentViewMode = modeSelect.value;

    if (btnAll) {
        btnAll.classList.toggle("active", currentViewMode === "all");
    }

    if (currentViewMode === "section") {
        if (branchGroup) branchGroup.style.display = "inline-flex";
        if (yearGroup) yearGroup.style.display = "inline-flex";
        if (sectionGroup) sectionGroup.style.display = "inline-flex";
        if (targetGroup) targetGroup.style.display = "none";

        const branchSelect = document.getElementById("filterBranchSelect");
        if (branchSelect && branchSelect.options.length <= 1) {
            populateBranchYearSectionFilters();
        } else {
            applyTimetableFilter();
        }
    } else if (currentViewMode === "all") {
        if (branchGroup) branchGroup.style.display = "none";
        if (yearGroup) yearGroup.style.display = "none";
        if (sectionGroup) sectionGroup.style.display = "none";
        if (targetGroup) targetGroup.style.display = "none";
        applyTimetableFilter();
    } else {
        // Teacher or Room views
        if (branchGroup) branchGroup.style.display = "none";
        if (yearGroup) yearGroup.style.display = "none";
        if (sectionGroup) sectionGroup.style.display = "none";
        if (targetGroup) targetGroup.style.display = "inline-flex";

        let optionsHtml = `<option value="all">-- All --</option>`;
        const uniqueValues = new Set();

        if (lastGeneratedTimetable) {
            lastGeneratedTimetable.forEach(d => {
                d.slots.forEach(s => {
                    const sessList = (s.sessions && s.sessions.length > 0)
                        ? s.sessions
                        : (s.slot_type === "Teaching" && s.subject !== "Free" ? [s] : []);

                    sessList.forEach(sess => {
                        if (currentViewMode === "teacher" && sess.teacher && sess.teacher !== "-") uniqueValues.add(sess.teacher);
                        if (currentViewMode === "room" && sess.room && sess.room !== "-") uniqueValues.add(sess.room);
                    });
                });
            });
        }

        if (currentViewMode === "teacher") {
            if (targetLabel) targetLabel.innerHTML = `<i class="fa-solid fa-user-tie"></i> Faculty:`;
            Array.from(uniqueValues).sort().forEach(t => {
                optionsHtml += `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`;
            });
        } else if (currentViewMode === "room") {
            if (targetLabel) targetLabel.innerHTML = `<i class="fa-solid fa-door-open"></i> Room / Lab:`;
            Array.from(uniqueValues).sort().forEach(r => {
                optionsHtml += `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`;
            });
        }

        if (targetSelect) {
            targetSelect.innerHTML = optionsHtml;
            currentTargetFilter = targetSelect.value || "all";
        }

        applyTimetableFilter();
    }
}

function onFilterTargetChange() {
    const targetSelect = document.getElementById("filterTargetSelect");
    if (targetSelect) {
        currentTargetFilter = targetSelect.value;
        applyTimetableFilter();
    }
}

function filterTimetable() {
    const secSelect = document.getElementById("sectionFilter");
    if (secSelect) {
        const modeSelect = document.getElementById("viewModeSelect");
        if (modeSelect) modeSelect.value = "section";
        selectedSection = secSelect.value;
        const filterSec = document.getElementById("filterSectionSelect");
        if (filterSec) filterSec.value = selectedSection;
        onViewModeChange();
    }
}

function getFilteredSessions(slot) {
    let sessList = [];
    if (slot.sessions && slot.sessions.length > 0) {
        sessList = slot.sessions;
    } else if (slot.subject && slot.subject !== "Free" && slot.subject !== "Break/Free" && slot.subject !== "Break") {
        sessList = [{
            subject: slot.subject,
            teacher: slot.teacher,
            room: slot.room,
            section: slot.section,
            branch: slot.branch || "",
            year: slot.year || "",
            section_letter: slot.section_letter || ""
        }];
    }

    if (currentViewMode === "teacher" && currentTargetFilter !== "all") {
        return sessList.filter(s => s.teacher === currentTargetFilter);
    }
    if (currentViewMode === "room" && currentTargetFilter !== "all") {
        return sessList.filter(s => s.room === currentTargetFilter);
    }
    if (currentViewMode === "section") {
        if (selectedSection && selectedSection !== "all") {
            return sessList.filter(s => (s.section || "").toLowerCase() === selectedSection.toLowerCase());
        }
        if (selectedBranch !== "all") {
            sessList = sessList.filter(s => {
                const sec = (lastGeneratedSectionsInfo || []).find(secItem => secItem.name.toLowerCase() === (s.section || "").toLowerCase());
                const branch = s.branch || (sec && sec.branch) || "";
                return branch.trim().toLowerCase() === selectedBranch.trim().toLowerCase();
            });
        }
        if (selectedYear !== "all") {
            sessList = sessList.filter(s => {
                const sec = (lastGeneratedSectionsInfo || []).find(secItem => secItem.name.toLowerCase() === (s.section || "").toLowerCase());
                const yr = s.year || (sec && sec.year) || "";
                return yr.trim().toLowerCase() === selectedYear.trim().toLowerCase();
            });
        }
    }
    return sessList;
}

function renderValidationReport(scope = "all") {
    const sectionEl = document.getElementById("validationReportSection");
    const tbody = document.getElementById("validationReportTbody");
    const summaryBadge = document.getElementById("validationReportSummaryBadge");
    const badgeCpsat = document.getElementById("badgeCpsatVerified");
    if (!sectionEl || !tbody) return;

    if (!scope || timetableInvalidated || !Array.isArray(lastGeneratedValidationReport) || lastGeneratedValidationReport.length === 0) {
        sectionEl.style.display = "none";
        return;
    }

    // Filter report rows based on selected scope
    let items = lastGeneratedValidationReport;
    if (scope && scope !== "all" && scope !== "All Timetables") {
        items = lastGeneratedValidationReport.filter(item => 
            (item.section && item.section.trim().toLowerCase() === scope.trim().toLowerCase())
        );
    }

    if (items.length === 0) {
        items = lastGeneratedValidationReport;
    }

    sectionEl.style.display = "block";

    const totalItems = items.length;
    const passedItems = items.filter(i => i.status === "PASSED").length;
    const allPassed = (passedItems === totalItems);

    if (summaryBadge) {
        if (allPassed) {
            summaryBadge.className = "validation-summary-pill passed";
            summaryBadge.innerHTML = `<i class="fa-solid fa-circle-check"></i> ${passedItems}/${totalItems} Subjects Verified (100% Period Fulfillment)`;
        } else {
            summaryBadge.className = "validation-summary-pill failed";
            summaryBadge.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> ${totalItems - passedItems} Subject(s) Missing Required Periods`;
        }
    }

    // Update global CP-SAT badge
    if (badgeCpsat) {
        if (allPassed) {
            badgeCpsat.className = "status-badge badge-cpsat";
            badgeCpsat.style.cssText = "";
            badgeCpsat.innerHTML = `<i class="fa-solid fa-microchip"></i> CP-SAT Verified`;
            badgeCpsat.title = "Integer Programming Constraint Satisfaction Verified (0 Gaps & Complete Period Fulfillment)";
        } else {
            badgeCpsat.className = "status-badge badge-unverified";
            badgeCpsat.style.cssText = "background:#fee2e2;color:#991b1b;border:1px solid #f87171;";
            badgeCpsat.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> Period Mismatch`;
            badgeCpsat.title = "Required weekly periods not fully satisfied";
        }
    }

    tbody.innerHTML = items.map(item => {
        const isPassed = (item.status === "PASSED");
        const durLabel = (item.duration > 1) ? `${item.duration} Periods (Consecutive)` : "1 Period";
        const statusBadge = isPassed
            ? `<span class="badge-pill-passed"><i class="fa-solid fa-circle-check"></i> PASSED</span>`
            : `<span class="badge-pill-failed"><i class="fa-solid fa-circle-xmark"></i> FAILED (${escapeHtml(item.reason || "Short")})</span>`;

        return `
            <tr class="${isPassed ? 'row-passed' : 'row-failed'}">
                <td>
                    <strong>${escapeHtml(item.subject)}</strong>
                    <div style="font-size:0.75rem;color:var(--muted)">${escapeHtml(item.type || 'Classroom')}</div>
                </td>
                <td><code>${escapeHtml(item.code)}</code></td>
                <td>${escapeHtml(item.teacher)}</td>
                <td><span class="badge-section-tag">${escapeHtml(item.section)}</span></td>
                <td>${durLabel}</td>
                <td style="text-align:center;font-weight:600">${item.required}</td>
                <td style="text-align:center;font-weight:700;color:${isPassed ? '#059669' : '#dc2626'}">${item.generated}</td>
                <td>${statusBadge}</td>
            </tr>
        `;
    }).join("");
}

function applyTimetableFilter() {
    const banner = document.getElementById("activeTimetableHeader");

    if (timetableInvalidated || !lastGeneratedTimetable || lastGeneratedTimetable.length === 0) {
        if (banner) banner.innerHTML = "";
        render(null);
        renderValidationReport(null);
        return;
    }

    if (currentViewMode === "all") {
        if (banner) {
            const secCount = (lastGeneratedSectionsInfo && lastGeneratedSectionsInfo.length) || Object.keys(lastGeneratedSectionTimetables).length || "All";
            const allPassed = (!lastGeneratedValidationReport || lastGeneratedValidationReport.length === 0) ||
                lastGeneratedValidationReport.every(item => item.status === "PASSED");
            const cpsatBadgeHtml = allPassed
                ? `<span class="meta-pill highlight"><i class="fa-solid fa-circle-check"></i> Zero Clashes (CP-SAT Verified)</span>`
                : `<span class="meta-pill text-rose" style="border-color:#fca5a5;background:#fee2e2;color:#991b1b;"><i class="fa-solid fa-triangle-exclamation"></i> Period Mismatch</span>`;

            banner.innerHTML = `
                <div class="active-title-group">
                    <i class="fa-solid fa-calendar-days"></i>
                    <div>
                        <h3>All Timetables &mdash; Master Combined College Schedule</h3>
                        <span style="font-size:0.8rem;color:#475569">Global conflict-free schedule across all branches, academic years, and sections</span>
                    </div>
                </div>
                <div class="active-meta-badges">
                    <span class="meta-pill"><i class="fa-solid fa-building-columns"></i> College Overview</span>
                    <span class="meta-pill"><i class="fa-solid fa-layer-group"></i> ${secCount} Sections Included</span>
                    ${cpsatBadgeHtml}
                </div>
            `;
        }
        render(lastGeneratedTimetable);
        renderValidationReport("all");
        return;
    }

    if (currentViewMode === "teacher") {
        const isAll = currentTargetFilter === "all";
        if (banner) {
            banner.innerHTML = `
                <div class="active-title-group">
                    <i class="fa-solid fa-user-tie"></i>
                    <div>
                        <h3>${isAll ? "All Faculty Timetable" : "Faculty Schedule: " + escapeHtml(currentTargetFilter)}</h3>
                        <span style="font-size:0.8rem;color:#475569">${isAll ? "Overview of classes for all instructors" : "Individual teaching timetable for " + escapeHtml(currentTargetFilter)}</span>
                    </div>
                </div>
                <div class="active-meta-badges">
                    <span class="meta-pill"><i class="fa-solid fa-chalkboard-user"></i> Faculty View</span>
                    ${!isAll ? `<span class="meta-pill"><i class="fa-solid fa-user"></i> ${escapeHtml(currentTargetFilter)}</span>` : ""}
                    <span class="meta-pill highlight"><i class="fa-solid fa-circle-check"></i> No Clash</span>
                </div>
            `;
        }
        render(lastGeneratedTimetable);
        renderValidationReport(currentTargetFilter === "all" ? "all" : null);
        return;
    }

    if (currentViewMode === "room") {
        const isAll = currentTargetFilter === "all";
        if (banner) {
            banner.innerHTML = `
                <div class="active-title-group">
                    <i class="fa-solid fa-door-open"></i>
                    <div>
                        <h3>${isAll ? "All Venues Timetable" : "Venue Schedule: " + escapeHtml(currentTargetFilter)}</h3>
                        <span style="font-size:0.8rem;color:#475569">${isAll ? "Overview of class usage across all rooms and labs" : "Occupancy schedule for " + escapeHtml(currentTargetFilter)}</span>
                    </div>
                </div>
                <div class="active-meta-badges">
                    <span class="meta-pill"><i class="fa-solid fa-location-dot"></i> Venue View</span>
                    ${!isAll ? `<span class="meta-pill"><i class="fa-solid fa-door-closed"></i> ${escapeHtml(currentTargetFilter)}</span>` : ""}
                    <span class="meta-pill highlight"><i class="fa-solid fa-circle-check"></i> Capacity Verified</span>
                </div>
            `;
        }
        render(lastGeneratedTimetable);
        renderValidationReport(currentTargetFilter === "all" ? "all" : null);
        return;
    }

    // Default: currentViewMode === "section"
    if (!selectedSection) {
        populateBranchYearSectionFilters();
    }
    const secDays = lastGeneratedSectionTimetables[selectedSection];
    const secInfo = (lastGeneratedSectionsInfo || []).find(s => s.name === selectedSection) || {
        name: selectedSection,
        branch: selectedBranch || "General",
        year: selectedYear || "General",
        section: selectedSection,
        strength: "--"
    };

    const secPassed = (!lastGeneratedValidationReport || lastGeneratedValidationReport.length === 0) ||
        lastGeneratedValidationReport.filter(item => (item.section || "").trim().toLowerCase() === (secInfo.name || "").trim().toLowerCase())
            .every(item => item.status === "PASSED");
    const secCpsatBadgeHtml = secPassed
        ? `<span class="meta-pill highlight"><i class="fa-solid fa-circle-check"></i> 0 Gaps &bull; CP-SAT Verified</span>`
        : `<span class="meta-pill text-rose" style="border-color:#fca5a5;background:#fee2e2;color:#991b1b;"><i class="fa-solid fa-triangle-exclamation"></i> Period Verification Failed</span>`;

    if (banner) {
        banner.innerHTML = `
            <div class="active-title-group">
                <i class="fa-solid fa-graduation-cap"></i>
                <div>
                    <h3>
                        <span>${escapeHtml(secInfo.branch || "Branch")}</span>
                        <i class="fa-solid fa-arrow-right flow-sep" style="font-size:0.8rem;color:#64748b;margin:0 6px;"></i>
                        <span>${escapeHtml(secInfo.year || "Year")}</span>
                        <i class="fa-solid fa-arrow-right flow-sep" style="font-size:0.8rem;color:#64748b;margin:0 6px;"></i>
                        <span style="color:var(--p)">Section ${escapeHtml(secInfo.section || secInfo.name)}</span>
                    </h3>
                    <span style="font-size:0.8rem;color:#475569">Displaying <strong>ONLY</strong> Section ${escapeHtml(secInfo.section || secInfo.name)} (${escapeHtml(secInfo.name)}) Timetable &bull; Compact &amp; Conflict-Free</span>
                </div>
            </div>
            <div class="active-meta-badges">
                <span class="meta-pill"><i class="fa-solid fa-code-branch"></i> ${escapeHtml(secInfo.branch || "Branch")}</span>
                <span class="meta-pill"><i class="fa-solid fa-calendar"></i> ${escapeHtml(secInfo.year || "Year")}</span>
                <span class="meta-pill"><i class="fa-solid fa-users"></i> Section ${escapeHtml(secInfo.section || secInfo.name)}</span>
                <span class="meta-pill"><i class="fa-solid fa-user-group"></i> Strength: ${escapeHtml(secInfo.strength || "--")}</span>
                ${secCpsatBadgeHtml}
            </div>
        `;
    }

    if (secDays && secDays.length > 0) {
        render(secDays);
    } else {
        render(lastGeneratedTimetable);
    }
    renderValidationReport(selectedSection);
}

// ==============================================================================
// Timetable Rendering & Matrix Layout
// ==============================================================================
function render(days) {
    const tableHead = document.querySelector("#table thead");
    const tableBody = document.querySelector("#table tbody");
    const alertBox = document.getElementById("timetableAlert");
    if (!tableBody) return;

    if (alertBox) {
        if (!timetableInvalidated || (days && days.length > 0)) {
            alertBox.innerHTML = "";
        } else {
            alertBox.innerHTML = `
                <div class="status warn" style="margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">
                    <div>
                        <i class="fa-solid fa-triangle-exclamation" style="margin-right:8px;color:#d97706"></i>
                        <strong>${escapeHtml(timetableInvalidationMessage || "Data changed — please regenerate the timetable.")}</strong>
                        <span style="display:block;font-size:0.85rem;color:var(--muted);margin-top:2px;">Underlying institutional resources were updated. Previous timetable is no longer valid.</span>
                    </div>
                    <button class="primary" style="padding:6px 14px;font-size:0.85rem;" onclick="page('solver')">
                        <i class="fa-solid fa-rotate"></i> Regenerate Now
                    </button>
                </div>
            `;
        }
    }

    // Determine active slot columns strictly from generated timetable or database time slots
    let activeSlots = [];
    if (days && days[0] && Array.isArray(days[0].slots) && days[0].slots.length > 0) {
        activeSlots = days[0].slots.map(s => s.time);
    } else if (timeSlots && timeSlots.length > 0) {
        activeSlots = timeSlots;
    }

    // Helper to check if a slot is marked as break
    const isSlotBreak = (slotTime) => {
        const found = activeTimeSlotObjects.find(s => s.time === slotTime);
        if (found && found.type && found.type.toLowerCase() === "break") return true;
        if (days && days[0] && Array.isArray(days[0].slots)) {
            const ds = days[0].slots.find(s => s.time === slotTime);
            if (ds && ((ds.slot_type && ds.slot_type.toLowerCase() === "break") || (ds.subject && ds.subject.toLowerCase() === "break"))) {
                return true;
            }
        }
        return false;
    };

    // Update live schedule summary strip metrics
    let totalTheory = 0;
    let totalLabs = 0;
    let totalBreaks = 0;

    if (days && Array.isArray(days)) {
        for (const d of days) {
            for (const s of (d.slots || [])) {
                const isBreak = (s.slot_type && s.slot_type.toLowerCase() === "break") ||
                                (s.subject && s.subject.toLowerCase() === "break") ||
                                isSlotBreak(s.time);
                if (isBreak) {
                    totalBreaks++;
                } else {
                    const activeSessions = getFilteredSessions(s);
                    for (const sess of activeSessions) {
                        const sessType = (sess.session_type || "").trim().toLowerCase();
                        const isLab = sessType === "lab" || (!sessType && (sess.subject || "").toLowerCase().includes("lab")) || 
                                      (sess.room && sess.room.toLowerCase().includes("lab"));
                        if (isLab) totalLabs++;
                        else totalTheory++;
                    }
                }
            }
        }
    }

    const elSlots = document.getElementById("ttSummarySlots");
    const elTheory = document.getElementById("ttSummaryTheory");
    const elLabs = document.getElementById("ttSummaryLabs");
    const elBreaks = document.getElementById("ttSummaryBreaks");
    const elConflicts = document.getElementById("ttSummaryConflicts");

    if (elSlots) elSlots.textContent = activeSlots.length > 0 ? activeSlots.length : "--";
    if (elTheory) elTheory.textContent = days && days.length > 0 ? totalTheory : "--";
    if (elLabs) elLabs.textContent = days && days.length > 0 ? totalLabs : "--";
    if (elBreaks) elBreaks.textContent = days && days.length > 0 ? totalBreaks : "--";
    if (elConflicts) {
        if (days && days.length > 0) {
            elConflicts.textContent = "0 Clashes";
            elConflicts.classList.add("text-emerald");
        } else {
            elConflicts.textContent = "Pending Generation";
            elConflicts.classList.remove("text-emerald");
        }
    }

    // If no time slots are available from database or timetable, show a clear error message
    if (activeSlots.length === 0) {
        if (tableHead) {
            tableHead.innerHTML = `<tr><th class="timetable-corner-header">Day / Time</th></tr>`;
        }
        tableBody.innerHTML = `
            <tr>
                <td style="text-align:center;padding:56px 20px;color:var(--bad)">
                    <div style="font-size:2.5rem;margin-bottom:14px;color:var(--warn)">
                        <i class="fa-solid fa-triangle-exclamation"></i>
                    </div>
                    <h3 style="margin-bottom:8px;font-size:1.25rem;color:var(--dark)">No Time Slots Loaded from Database</h3>
                    <p style="max-width:540px;margin:0 auto 18px auto;color:var(--muted);font-size:0.95rem">
                        The timetable requires dynamic time intervals loaded from <code>/api/timeslots</code>. Please check your connection or add teaching and break slots in Time Slot Management.
                    </p>
                    <button class="primary" onclick="page('data'); loadTab('timeslots');">
                        <i class="fa-solid fa-clock"></i> Manage Time Slots
                    </button>
                </td>
            </tr>
        `;
        return;
    }

    const colCount = activeSlots.length + 1;
    const tableEl = document.getElementById("table");
    if (tableEl && activeSlots.length > 0) {
        tableEl.style.setProperty("--slot-count", activeSlots.length);
    }

    // Render table header dynamically to match backend slots
    if (tableHead) {
        tableHead.innerHTML = `
            <tr>
                <th class="timetable-corner-header">Day / Time</th>
                ${activeSlots.map(t => {
                    const isBreak = isSlotBreak(t);
                    return `
                        <th class="timetable-time-header">
                            <div class="time-header-content">
                                <span class="time-slot-label"><i class="fa-regular fa-clock"></i> ${escapeHtml(t)}</span>
                                <span class="time-slot-type-badge ${isBreak ? 'break' : ''}">${isBreak ? 'BREAK' : 'PERIOD'}</span>
                            </div>
                        </th>
                    `;
                }).join("")}
            </tr>
        `;
    }

    if (!days || !Array.isArray(days) || days.length === 0) {
        const emptyTitle = timetableInvalidated ? "Data Changed — Timetable Cleared" : "No Timetable Generated Yet";
        const emptyDesc = timetableInvalidated 
            ? "Data changed — please regenerate the timetable. Institutional resources were modified, so the previous timetable was cleared to maintain 100% data consistency."
            : `Ready to calculate an optimal, clash-free schedule across all configured ${activeSlots.length} dynamic time slots.`;
        const iconClass = timetableInvalidated ? "fa-solid fa-triangle-exclamation" : "fa-solid fa-wand-magic-sparkles";
        const iconColor = timetableInvalidated ? "var(--warn)" : "var(--p)";

        tableBody.innerHTML = `
            <tr>
                <td colspan="${colCount}" style="text-align:center;padding:56px 16px;color:var(--muted)">
                    <div style="font-size:2.5rem;margin-bottom:12px;color:${iconColor};opacity:0.9">
                        <i class="${iconClass}"></i>
                    </div>
                    <h3 style="margin-bottom:8px;font-size:1.2rem;color:var(--dark)">${escapeHtml(emptyTitle)}</h3>
                    <p style="max-width:540px;margin:0 auto 20px auto;color:var(--muted)">
                        ${escapeHtml(emptyDesc)}
                    </p>
                    <button class="primary" onclick="page('solver')">
                        <i class="fa-solid fa-rotate"></i> Regenerate Timetable
                    </button>
                </td>
            </tr>
        `;
        return;
    }

    // Build grid rows for each day
    let html = "";
    for (const d of days) {
        html += `
            <tr>
                <th class="timetable-day-header">
                    <div class="day-header-badge">
                        <span class="day-name">${escapeHtml(d.day)}</span>
                    </div>
                </th>
        `;
        
        const slots = d.slots || [];
        let i = 0;
        while (i < slots.length) {
            const s = slots[i];
            const isBreak = (s.slot_type && s.slot_type.toLowerCase() === "break") ||
                            (s.subject && s.subject.toLowerCase() === "break") ||
                            isSlotBreak(s.time);

            if (isBreak) {
                html += `
                    <td class="timetable-break-cell">
                        <div class="break-card-elegant" title="Scheduled Institutional Break (${escapeHtml(s.time)})">
                            <div class="break-icon-wrap"><i class="fa-solid fa-mug-hot"></i></div>
                            <div class="break-label">BREAK</div>
                            <div class="break-sub">Institutional Pause</div>
                        </div>
                    </td>
                `;
                i++;
                continue;
            }

            // Extract active sessions for this teaching slot based on active filters
            const sessions = getFilteredSessions(s);

            if (sessions.length === 0) {
                html += `
                    <td class="timetable-free-cell">
                        <div class="free-card-elegant" title="No class scheduled for this period">
                            <i class="fa-regular fa-clock"></i> <span>Free Period</span>
                        </div>
                    </td>
                `;
                i++;
                continue;
            }

            // Check if this slot contains a single session that can be merged with consecutive identical sessions
            let span = 1;
            if (sessions.length === 1) {
                const targetSess = sessions[0];
                while (i + span < slots.length) {
                    const nextSlot = slots[i + span];
                    const nextIsBreak = (nextSlot.slot_type && nextSlot.slot_type.toLowerCase() === "break") ||
                                        (nextSlot.subject && nextSlot.subject.toLowerCase() === "break") ||
                                        isSlotBreak(nextSlot.time);
                    if (nextIsBreak) break;

                    const nextSessions = getFilteredSessions(nextSlot);
                    if (nextSessions.length !== 1) break;

                    const ns = nextSessions[0];
                    if (
                        ns.subject === targetSess.subject &&
                        ns.teacher === targetSess.teacher &&
                        ns.room === targetSess.room &&
                        (ns.section || "") === (targetSess.section || "")
                    ) {
                        span++;
                    } else {
                        break;
                    }
                }
            }

            // Compute time span if merged
            const startSlotTime = slots[i].time;
            const endSlotTime = slots[i + span - 1].time;
            let timeSpanText = startSlotTime;
            if (span > 1) {
                const startTime = (startSlotTime.split("-")[0] || startSlotTime).trim();
                const endTime = endSlotTime.includes("-") ? endSlotTime.split("-")[1].trim() : endSlotTime.trim();
                timeSpanText = `${startTime} - ${endTime}`;
            }

            const tdColspan = span > 1 ? ` colspan="${span}"` : "";
            html += `<td${tdColspan}>`;

            sessions.forEach(sess => {
                const sessType = (sess.session_type || "").trim().toLowerCase();
                const isLab = sessType === "lab" || (!sessType && (sess.subject || "").toLowerCase().includes("lab")) || 
                              (sess.room && sess.room.toLowerCase().includes("lab"));
                const isLibrary = sessType === "library" || (!sessType && (sess.subject || "").toLowerCase().includes("library")) || 
                                  (sess.room && sess.room.toLowerCase().includes("library"));

                let roomIcon = "fa-door-open";
                let cardClass = "session-card session-card-theory";
                let typeBadge = `<span class="session-type-pill theory"><i class="fa-solid fa-book-open"></i> CLASSROOM</span>`;

                if (isLibrary) {
                    roomIcon = "fa-book-bookmark";
                    cardClass = "session-card session-card-library";
                    typeBadge = `<span class="session-type-pill library"><i class="fa-solid fa-book-bookmark"></i> LIBRARY</span>`;
                } else if (isLab) {
                    roomIcon = "fa-flask-vial";
                    cardClass = "session-card session-card-lab";
                    typeBadge = `<span class="session-type-pill lab"><i class="fa-solid fa-flask"></i> LAB</span>`;
                }

                if (span > 1) {
                    cardClass += " session-card-continuous";
                }

                const durationBadge = span > 1 ? 
                    `<span class="session-duration-pill" title="Continuous Session: ${span} Hours (${escapeHtml(timeSpanText)})"><i class="fa-solid fa-arrows-left-right"></i> ${span}h (${escapeHtml(timeSpanText)})</span>` : "";

                html += `
                    <div class="${cardClass}">
                        <div class="session-top">
                            <div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;">
                                ${typeBadge}
                                ${durationBadge}
                            </div>
                            ${sess.section ? `<span class="session-section" title="Section ${escapeHtml(sess.section)}">${escapeHtml(sess.section)}</span>` : ''}
                        </div>
                        <div class="session-subject" title="${escapeHtml(sess.subject)}">${escapeHtml(sess.subject)}</div>
                        <div class="session-meta">
                            <div class="session-teacher" title="Faculty: ${escapeHtml(sess.teacher || 'TBA')}">
                                <i class="fa-solid fa-user-tie"></i> <span>${escapeHtml(sess.teacher || 'TBA')}</span>
                            </div>
                            <div class="session-room" title="${isLab ? 'Laboratory' : 'Classroom'}: ${escapeHtml(sess.room || 'TBA')}">
                                <i class="fa-solid ${roomIcon}"></i> <span>${escapeHtml(sess.room || 'TBA')}</span>
                            </div>
                        </div>
                    </div>
                `;
            });
            html += `</td>`;

            i += span;
        }
        
        html += `</tr>`;
    }

    tableBody.innerHTML = html;

    const scrollWrap = document.querySelector(".timetable-scroll-wrap");
    if (scrollWrap) {
        scrollWrap.scrollLeft = 0;
    }
}

function showTimetableError(summary, details) {
    const alertBox = document.getElementById("timetableAlert");
    if (alertBox) {
        alertBox.innerHTML = `
            <div class="status error" style="margin-bottom:16px">
                <strong>${escapeHtml(summary || "Timetable Generation Notice")}:</strong> ${escapeHtml(details || "")}
            </div>
        `;
    }
    const tableBody = document.querySelector("#table tbody");
    const colCount = (timeSlots && timeSlots.length > 0 ? timeSlots.length : 1) + 1;
    if (tableBody) {
        tableBody.innerHTML = `
            <tr>
                <td colspan="${colCount}" style="text-align:center;padding:36px;color:var(--bad)">
                    <strong>${escapeHtml(summary || "Unable to display timetable")}:</strong> ${escapeHtml(details || "")}
                    <div style="margin-top: 14px;">
                        <button class="secondary" onclick="page('solver')">Return to Solver</button>
                    </div>
                </td>
            </tr>
        `;
    }
}

// ==============================================================================
// Initializer
// ==============================================================================
document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".nav button").forEach(b => {
        b.onclick = () => page(b.dataset.page);
    });

    const modalForm = document.getElementById("crudModalForm");
    if (modalForm) {
        modalForm.onsubmit = (e) => {
            e.preventDefault();
            e.stopPropagation();
            handleModalSubmit(e);
            return false;
        };
    }

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            closeCrudModal();
        }
    });

    checkSession();
});
