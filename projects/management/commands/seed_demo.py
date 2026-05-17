"""Populate the database with a fully-featured demo project.

Usage::

    python manage.py seed_demo            # idempotent: skips already-created bits
    python manage.py seed_demo --clear    # wipe the DEMO project first, then reseed

What it creates:
- Issue types, statuses, priorities (if missing — same as ``seed_jirrabit``).
- 5 demo users with stable passwords and avatars.
- A project ``DEMO`` ("Plataforma e-commerce") with all users as members.
- 6 labels (frontend/backend/db/ux/qa/devops).
- 3 sprints (closed, active, future) plus 2 epics ("Pricing engine",
  "Checkout v2").
- ~14 issues spread across the sprints with realistic types, priorities,
  statuses, story points, due dates, watchers, subtasks, comments, history
  entries and a couple of text attachments.

Signals that send notification emails are temporarily disconnected so the
console backend doesn't flood stdout while the demo is loading.
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models.signals import post_save
from django.utils import timezone

from accounts.models import User
from core import notifications
from issues.models import (
    Attachment,
    Comment,
    HistoryEntry,
    Issue,
    IssueType,
    Label,
    Priority,
    Status,
)
from projects.models import Epic, Project, Sprint

PROJECT_KEY = "DEMO"

# (username, display_name, email, job_title, password, avatar_color)
DEMO_USERS = [
    ("alice_pm", "Alice Pérez", "alice@demo.test", "Product Manager", "demopass", "#1e6fff"),
    ("bob_dev", "Bob Vega", "bob@demo.test", "Backend Engineer", "demopass", "#16a34a"),
    ("carol_dev", "Carol Núñez", "carol@demo.test", "Frontend Engineer", "demopass", "#f97316"),
    ("dave_qa", "Dave Lara", "dave@demo.test", "QA Engineer", "demopass", "#dc2626"),
    ("erin_ux", "Erin Soto", "erin@demo.test", "Designer", "demopass", "#7c3aed"),
]

DEMO_LABELS = [
    ("frontend", "#1e6fff"),
    ("backend", "#16a34a"),
    ("db", "#f97316"),
    ("ux", "#7c3aed"),
    ("qa", "#dc2626"),
    ("devops", "#0ea5e9"),
]

DEMO_EPICS = [
    ("Pricing engine", "Cálculo dinámico de precios y descuentos.", "#7c3aed"),
    ("Checkout v2", "Rediseño completo del flujo de pago y envío.", "#1e6fff"),
]

# (summary, type, priority, status, story_points, epic_idx, label_idxs, description, due_offset_days)
DEMO_ISSUES = [
    ("Calcular descuentos por cliente VIP", "Story", "High", "Done", 5, 0, [1, 2],
     "Implementar tier VIP con descuento automático del 10% sobre líneas no rebajadas.", -3),
    ("Endpoint /api/price/preview", "Task", "Medium", "Done", 3, 0, [1],
     "Devolver el precio simulado para una cesta sin persistir.", -1),
    ("Cache de reglas de precio en Redis", "Task", "High", "In Progress", 5, 0, [1, 5],
     "Cargar reglas en caché con TTL de 5 min para no machacar la BD.", 2),
    ("Bug: redondeo a 2 decimales en IVA", "Bug", "Highest", "In Progress", 2, 0, [1, 2],
     "Los importes con IVA 21% se desvían 1 cent en algunos productos.", 1),
    ("Auditoría de cambios en tarifa", "Story", "Medium", "To Do", 8, 0, [1],
     "Guardar historial de cambios en `PriceList` para cumplir SOX.", 7),
    ("Diseño del paso 'Dirección de envío'", "Story", "High", "Done", 5, 1, [3],
     "Mockups en Figma + tokens de espaciado.", -5),
    ("Validación asíncrona de código postal", "Task", "Medium", "In Review", 3, 1, [0, 1],
     "Llamada al servicio externo CodPostal y debounce 400ms en el input.", 0),
    ("Soportar Apple Pay en checkout", "Story", "High", "In Progress", 13, 1, [0, 1],
     "Integrar Apple Pay JS y verificación de comerciante.", 4),
    ("Bug: doble click en 'Pagar' duplica el pedido", "Bug", "Highest", "Blocked", 3, 1, [0, 1, 4],
     "Hace falta lock de submit; reproducible en Safari iOS 17.", 1),
    ("Tests E2E de checkout con Playwright", "Task", "Medium", "To Do", 5, 1, [4],
     "Cobertura mínima 80% del happy path + 3 errores.", 9),
    ("Métrica de abandono por paso", "Task", "Low", "To Do", 3, 1, [5],
     "Eventos a Mixpanel: viewed_step, submitted_step, errored_step.", 12),
    ("Bug: timeout al consultar stock", "Bug", "High", "In Progress", 2, None, [1, 2, 5],
     "Consulta sin índice; afecta 0.3% de requests.", 0),
    ("Refactor de Order.save() en celdas atómicas", "Task", "Medium", "To Do", 5, None, [1],
     "Separar en transitions explícitas usando `django-fsm`.", 14),
    ("Documentar el SDK de pagos para partners", "Story", "Low", "To Do", 3, None, [],
     "README + ejemplos cURL + esquema OpenAPI.", 21),
]

COMMENT_TEMPLATES = [
    "Voy a por esto hoy, lo dejo en review esta tarde.",
    "Ya tengo el PR abierto: ver rama feature/{slug}.",
    "He hablado con producto, hay que ajustar el criterio de aceptación.",
    "Bloqueado por la integración con el proveedor — esperando respuesta.",
    "QA: probado en staging, OK. Pasa a Done.",
    "Esto tiene impacto en performance, abramos un spike antes.",
    "@alice_pm ¿confirmas el copy del botón?",
]


def _disconnect_notifications():
    # ``Signal.disconnect`` matches on (receiver, sender, dispatch_uid).
    # Need both ``sender`` and ``dispatch_uid`` for the lookup key to match.
    pairs = [
        (Issue, "notify_issue"),
        (Comment, "notify_comment"),
        (Attachment, "notify_attachment"),
        (Project, "notify_project"),
        (Epic, "notify_epic"),
        (Sprint, "notify_sprint"),
        (User, "notify_user"),
    ]
    for sender, uid in pairs:
        post_save.disconnect(sender=sender, dispatch_uid=uid)


def _reconnect_notifications():
    notifications.connect()


def _ensure_base_lookups():
    types = [
        ("Epic", "epic", "▲", "#7c3aed"),
        ("Story", "story", "★", "#22c55e"),
        ("Task", "task", "✓", "#1e6fff"),
        ("Bug", "bug", "✦", "#ef4444"),
        ("Subtask", "subtask", "↳", "#0ea5e9"),
    ]
    for name, cat, icon, color in types:
        IssueType.objects.get_or_create(
            name=name, defaults={"category": cat, "icon": icon, "color": color}
        )
    statuses = [
        ("To Do", "todo", 10),
        ("In Progress", "in_progress", 20),
        ("In Review", "in_progress", 30),
        ("Blocked", "in_progress", 40),
        ("Done", "done", 50),
    ]
    for name, cat, order in statuses:
        Status.objects.get_or_create(name=name, defaults={"category": cat, "order": order})
    priorities = [
        ("Highest", 50, "#dc2626"),
        ("High", 40, "#f97316"),
        ("Medium", 30, "#1e6fff"),
        ("Low", 20, "#22c55e"),
        ("Lowest", 10, "#94a3b8"),
    ]
    for name, w, color in priorities:
        Priority.objects.get_or_create(name=name, defaults={"weight": w, "color": color})


def _svg_avatar(initials: str, color: str) -> str:
    import base64
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80">'
        f'<rect width="80" height="80" rx="40" fill="{color}"/>'
        f'<text x="40" y="48" text-anchor="middle" font-family="sans-serif" '
        f'font-size="32" font-weight="700" fill="white">{initials}</text></svg>'
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


class Command(BaseCommand):
    help = "Seed a demo project with sprints, epics, issues, comments and attachments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Wipe the DEMO project before reseeding.",
        )

    def handle(self, *args, **opts):
        random.seed(42)
        _disconnect_notifications()
        try:
            with transaction.atomic():
                if opts["clear"]:
                    self._clear()
                _ensure_base_lookups()
                users = self._users()
                project = self._project(users)
                labels = self._labels()
                sprints = self._sprints(project)
                epics = self._epics(project, users[0])
                self._issues(project, users, labels, sprints, epics)
        finally:
            _reconnect_notifications()
        self.stdout.write(self.style.SUCCESS(
            f"Demo listo. Login: alice_pm / demopass. Proyecto: /projects/{PROJECT_KEY}/"
        ))

    # ------------------------------------------------------------------
    def _clear(self):
        try:
            project = Project.objects.get(key=PROJECT_KEY)
        except Project.DoesNotExist:
            return
        Issue.objects.filter(project=project).delete()
        Epic.objects.filter(project=project).delete()
        Sprint.objects.filter(project=project).delete()
        project.delete()

    def _users(self) -> list[User]:
        created = []
        for username, display, email, job, password, color in DEMO_USERS:
            user, was_new = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": email,
                    "display_name": display,
                    "job_title": job,
                    "timezone": "Europe/Madrid",
                },
            )
            if was_new:
                user.set_password(password)
            user.email = email
            user.display_name = display
            user.job_title = job
            user.avatar = _svg_avatar(user.initials, color)
            if username == "alice_pm":
                user.is_staff = True
                user.is_superuser = True
            user.save()
            created.append(user)
        return created

    def _project(self, users: list[User]) -> Project:
        from projects.models import ProjectMembership
        project, _ = Project.objects.get_or_create(
            key=PROJECT_KEY,
            defaults={
                "name": "Plataforma e-commerce",
                "description": (
                    "Proyecto demo de jirrabit. Incluye sprints, epics, issues, "
                    "comentarios y adjuntos reales para mostrar el flujo completo."
                ),
                "lead": users[0],
            },
        )
        ProjectMembership.objects.filter(project=project).delete()
        roles = ["admin", "member", "member", "member", "member"]
        for user, role in zip(users, roles, strict=False):
            ProjectMembership.objects.create(project=project, user=user, role=role)
        return project

    def _labels(self) -> list[Label]:
        out = []
        for name, color in DEMO_LABELS:
            label, _ = Label.objects.get_or_create(name=name, defaults={"color": color})
            out.append(label)
        return out

    def _sprints(self, project: Project) -> dict[str, Sprint]:
        today = timezone.localdate()
        closed, _ = Sprint.objects.get_or_create(
            project=project,
            name="Sprint 11 — Onboarding",
            defaults={
                "goal": "Mejorar el alta de nuevos clientes B2B.",
                "start_date": today - timedelta(days=28),
                "end_date": today - timedelta(days=14),
                "status": "closed",
                "started_at": timezone.now() - timedelta(days=28),
                "closed_at": timezone.now() - timedelta(days=14),
            },
        )
        active, _ = Sprint.objects.get_or_create(
            project=project,
            name="Sprint 12 — Pricing & Checkout",
            defaults={
                "goal": "Sacar pricing dinámico y la primera fase de checkout v2.",
                "start_date": today - timedelta(days=7),
                "end_date": today + timedelta(days=7),
                "status": "active",
                "started_at": timezone.now() - timedelta(days=7),
            },
        )
        future, _ = Sprint.objects.get_or_create(
            project=project,
            name="Sprint 13 — Analytics",
            defaults={
                "goal": "Métricas de embudo y experimentación A/B.",
                "start_date": today + timedelta(days=8),
                "end_date": today + timedelta(days=22),
                "status": "future",
            },
        )
        return {"closed": closed, "active": active, "future": future}

    def _epics(self, project: Project, lead: User) -> list[Epic]:
        out = []
        for name, summary, color in DEMO_EPICS:
            epic, _ = Epic.objects.get_or_create(
                project=project,
                name=name,
                defaults={"summary": summary, "color": color, "created_by": lead},
            )
            out.append(epic)
        return out

    def _issues(self, project, users, labels, sprints, epics):
        type_map = {t.name: t for t in IssueType.objects.all()}
        prio_map = {p.name: p for p in Priority.objects.all()}
        status_map = {s.name: s for s in Status.objects.all()}
        today = timezone.now()

        created_issues = []
        for summary, type_name, prio_name, status_name, sp, epic_idx, label_idxs, desc, due_offset in DEMO_ISSUES:
            assignee = random.choice(users[1:])  # not alice (she's PM/reporter)
            sprint = sprints["closed"] if status_name == "Done" and random.random() < 0.5 else sprints["active"]
            issue, was_new = Issue.objects.get_or_create(
                project=project,
                summary=summary,
                defaults={
                    "issue_type": type_map[type_name],
                    "priority": prio_map[prio_name],
                    "status": status_map[status_name],
                    "story_points": sp,
                    "epic": epics[epic_idx] if epic_idx is not None else None,
                    "sprint": sprint,
                    "reporter": users[0],
                    "assignee": assignee,
                    "description": desc,
                    "due_date": (today + timedelta(days=due_offset)).date() if due_offset else None,
                    "resolved_at": today - timedelta(days=2) if status_name == "Done" else None,
                },
            )
            if was_new:
                issue.labels.set([labels[i] for i in label_idxs if i < len(labels)])
                # watchers: random subset of 1-3 users excluding assignee
                pool = [u for u in users if u != assignee]
                random.shuffle(pool)
                issue.watchers.set(pool[:random.randint(1, 3)])
                self._comments(issue, users, summary)
                self._history(issue, users, status_name)
                if random.random() < 0.3:
                    self._attachment(issue, random.choice(users))
            created_issues.append(issue)

        self._subtasks(created_issues[2], users, type_map, prio_map, status_map, sprints, project)

    def _comments(self, issue: Issue, users: list[User], summary: str):
        slug = summary.lower().replace(" ", "-")[:30]
        n = random.randint(1, 3)
        for _ in range(n):
            author = random.choice(users)
            body = random.choice(COMMENT_TEMPLATES).format(slug=slug)
            Comment.objects.create(issue=issue, author=author, body=body)

    def _history(self, issue: Issue, users: list[User], status_name: str):
        actor = users[0]
        HistoryEntry.objects.create(
            issue=issue, actor=actor, field="status",
            old_value="To Do", new_value=status_name,
        )
        if status_name == "Done":
            HistoryEntry.objects.create(
                issue=issue, actor=actor, field="resolved",
                old_value="", new_value="resuelto",
            )

    def _attachment(self, issue: Issue, user: User):
        import base64
        content = (
            f"Notas para {issue.key}\n"
            f"-----------------------\n"
            f"Resumen: {issue.summary}\n"
            f"Generado por seed_demo.\n"
        ).encode()
        Attachment.objects.create(
            issue=issue,
            uploaded_by=user,
            filename=f"{issue.key.lower()}-notas.txt",
            content_type="text/plain",
            size=len(content),
            data=base64.b64encode(content).decode("ascii"),
        )

    def _subtasks(self, parent: Issue, users, type_map, prio_map, status_map, sprints, project):
        subtasks = [
            ("Diseñar interfaz del cache layer", "In Progress"),
            ("Implementar invalidación por evento", "To Do"),
            ("Tests de carga con 1k rps", "To Do"),
        ]
        for title, status_name in subtasks:
            Issue.objects.get_or_create(
                project=project,
                summary=title,
                defaults={
                    "issue_type": type_map["Subtask"],
                    "priority": prio_map["Medium"],
                    "status": status_map[status_name],
                    "story_points": 2,
                    "epic": parent.epic,
                    "sprint": parent.sprint,
                    "parent": parent,
                    "reporter": parent.reporter,
                    "assignee": random.choice(users[1:]),
                    "description": f"Subtarea de {parent.key}.",
                },
            )
