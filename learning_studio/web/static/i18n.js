/*
 * UI strings, kept separate from exercise content.
 *
 * The distinction is the point. An exercise carries its own `content_locale`:
 * a French verb drill is in French whoever is doing it, and translating its
 * prompt would destroy the exercise. The *interface* around it -- "Submit",
 * "Card 2 of 5", "Answer this to continue" -- belongs to the reader, and comes
 * from here, chosen by the manifest's `ui_locale` and overridden by nothing.
 *
 * Resolution is deliberately forgiving in one direction only. `pt-BR` falls
 * back to `pt`, `pt` falls back to English, and an unknown or malformed tag
 * falls back to English -- never to a partially translated table, because a
 * card that is half French and half English is worse than one that is honestly
 * English. A locale is only offered if every key is present, which
 * `tests/test_mini_app_frontend.py` enforces rather than trusting.
 *
 * Interpolation is `{name}` and the result is only ever assigned to
 * `textContent`. There is no HTML in any string here and no path by which one
 * could become markup.
 *
 * Known limitation, stated rather than hidden: there is no plural machinery.
 * The strings are written to avoid count-dependent grammar ("Words: 12" rather
 * than "12 words"), which is enough for these six sentences and would not be
 * for a larger interface. `Intl.PluralRules` is the way in when it is needed.
 */

(function (global) {
  "use strict";

  var FALLBACK_LOCALE = "en";

  var STRINGS = {
    en: {
      "app.title": "Learning Studio",
      "app.skip": "Skip to the exercise",

      "meta.summary": "{minutes} min · {difficulty} · Cards: {count}",
      "difficulty.introductory": "Introductory",
      "difficulty.intermediate": "Intermediate",
      "difficulty.advanced": "Advanced",
      "difficulty.expert": "Expert",

      "progress.text": "Card {position} of {count}",
      "progress.label": "Progress through this exercise",

      "loading.title": "Opening your exercise…",
      "unsupported.title": "Open this from Telegram",
      "unsupported.body":
        "This exercise runs inside Telegram. Tap the button in the chat to open it.",
      "auth.title": "Telegram could not verify this launch",
      "auth.body": "Close this window and open the exercise again from the chat.",
      "forbidden.title": "Not available for this account",
      "forbidden.body": "This Telegram account is not set up to use the Learning Studio here.",
      "expired.title": "This session has ended",
      "expired.body": "Sessions are short by design. Reopen the exercise to carry on.",
      "notfound.title": "Exercise not found",
      "notfound.body": "It may have been removed, or it belongs to someone else.",
      "offline.title": "No connection",
      "offline.body": "Check your connection and try again.",
      "server.title": "Something went wrong",
      "server.body": "The Learning Studio could not finish that. Try again in a moment.",
      "throttled.title": "Slow down a moment",
      "throttled.body": "Too many requests. Wait a few seconds and try again.",
      "conflict.title": "That card has moved on",
      "conflict.body": "Reopen the exercise to pick up where you are.",
      "complete.title": "Exercise finished",
      "complete.body": "Answered: {answered} of {count}",
      "complete.score": "You got {correct} of {graded} marked answers right.",
      "complete.feedback.excellent": "Excellent work — nearly everything right. Keep it up!",
      "complete.feedback.good": "Well done! A few mistakes worth reviewing to make it stick.",
      "complete.feedback.mixed": "The foundations are there — take another look at what was missed.",
      "complete.feedback.rough": "A tricky exercise — reviewing the lesson before retrying will help.",
      "complete.feedback.to_review": "{count} question(s) to review.",
      "complete.no_score": "Nothing here is machine-graded. Your responses were recorded.",
      "complete.ungraded": "{count} more response(s) were recorded but are not machine-graded.",
      "complete.review": "Next review suggested: {date}",
      "complete.guidance": "Ask for another exercise any time to keep practising.",
      "complete.correct": "Correct",
      "complete.incorrect": "Incorrect",
      "complete.recorded_item": "Recorded",

      "action.submit": "Submit",
      "action.continue": "Continue",
      "action.retry": "Try again",
      "action.reopen": "Reopen",
      "action.close": "Close",

      "feedback.recorded": "Answer recorded.",

      "invalid.required": "Answer this to continue.",
      "invalid.choose_one": "Choose one option.",
      "invalid.choose_some": "Choose at least one option.",
      "invalid.all_blanks": "Fill in every blank.",
      "invalid.all_pairs": "Match every item.",
      "invalid.all_items": "Place every item.",
      "invalid.all_markers": "Label every marker.",
      "invalid.all_cells": "Complete every cell.",
      "invalid.all_steps": "Answer every step.",
      "invalid.all_prompts": "Respond to every prompt.",
      "invalid.point": "Tap the image to place your marker.",
      "invalid.rating": "Choose a rating.",
      "invalid.attempt_required": "Write what you remember before turning the card over.",
      "invalid.reveal_first": "Turn the card over before rating your recall.",
      "invalid.too_long": "That is too long to send. Shorten your answer and try again.",
      "invalid.min_words": "Write at least this many words: {count}",
      "invalid.max_words": "Keep this to at most this many words: {count}",

      "card.unsupported.title": "This card cannot be shown here",
      "card.unsupported.body":
        "This app does not know how to display a card of this kind yet. Ask for it in the chat instead.",
      "card.unsupported.type": "Card type: {type}",

      "card.type.multiple_choice": "Multiple choice",
      "card.type.multi_select": "Several answers",
      "card.type.true_false": "True or false",
      "card.type.classification": "Sorting",
      "card.type.categorization": "Grouping",
      "card.type.image_choice": "Pick an image",
      "card.type.scenario_choice": "Scenario",
      "card.type.decision_path": "Decision path",
      "card.type.case_study": "Case study",
      "card.type.fill_blank": "Fill the gaps",
      "card.type.short_answer": "Short answer",
      "card.type.typed_recall": "Recall",
      "card.type.free_response": "Written response",
      "card.type.rubric_response": "Guided response",
      "card.type.translation": "Translation",
      "card.type.error_correction": "Correction",
      "card.type.code_response": "Code",
      "card.type.sentence_order": "Word order",
      "card.type.sequence_order": "Put in order",
      "card.type.timeline": "Timeline",
      "card.type.process_flow": "Process",
      "card.type.matching": "Matching",
      "card.type.flashcard": "Flashcard",
      "card.type.image_observation": "Look closely",
      "card.type.diagram": "Diagram",
      "card.type.hotspot": "Point on the image",
      "card.type.labeling": "Labelling",
      "card.type.table_grid": "Table",
      "card.type.confidence_rating": "Confidence",
      "card.type.reflection": "Reflection",
      "card.type.self_explanation": "Explain it yourself",

      "card.true": "True",
      "card.false": "False",
      "card.statement": "Is this statement true?",
      "card.multi_hint": "Choose every option that applies.",
      "card.order_hint": "Put these in order using the up and down buttons.",
      "card.timeline_hint": "Put these in order, earliest first.",
      "card.categorization_hint": "Choose a group for every item.",
      "card.error_correction_hint": "Rewrite the passage with the mistakes corrected.",
      "card.move_up": "Move up",
      "card.move_down": "Move down",
      "card.position": "Position {position}",
      "card.choose": "Choose",
      "card.unchosen": "— choose —",
      "card.match_for": "Match for: {text}",
      "card.category_for": "Group for: {text}",
      "card.blank_label": "Blank: {label}",
      "card.blank_number": "Blank {number}",
      "card.answer_label": "Your answer",
      "card.words": "Words: {count}",
      "card.min_words": "At least this many words: {count}",
      "card.max_words": "At most this many words: {count}",
      "card.code_notice": "Your code is stored as text. It is never run.",
      "card.code_label": "Your code ({language})",
      "card.focus_points": "Look for:",
      "card.materials": "Materials:",
      "card.requirements": "Requirements:",
      "card.questions": "Questions:",
      "card.prompt_number": "Prompt {number}",
      "card.step_number": "Step {number}",
      "card.transcript": "Transcript",
      "card.long_description": "Full description",
      "card.keyboard_alternative": "Without pointing or dragging: {text}",
      "card.image_loading": "Loading image…",
      "card.image_failed": "The image could not be loaded. Its description is below.",
      "card.recall_label": "Write what you remember",
      "card.self_rate": "How well did you recall it?",
      "card.turn_over": "Turn the card over",
      "card.back_label": "The answer",
      "rate.again": "Again",
      "rate.hard": "Hard",
      "rate.good": "Good",
      "rate.easy": "Easy",
      "card.confidence": "How confident are you?",
      "card.rating_value": "Rating {value}",
      "card.hotspot_hint": "Tap the spot on the image.",
      "card.hotspot_keyboard": "Or use the arrow keys once the image has focus: {coarse}% a step, {fine}% with Shift.",
      "card.hotspot_selected": "Selected: {x}% across, {y}% down",
      "card.hotspot_clear": "Clear the marker",
      "card.hotspot_pick": "Choose a spot on the image",
      "card.marker_label": "Marker {number}",
      "card.grid_cell": "{row} — {column}",
    },

    fr: {
      "app.title": "Studio d'apprentissage",
      "app.skip": "Aller à l'exercice",

      "meta.summary": "{minutes} min · {difficulty} · Cartes : {count}",
      "difficulty.introductory": "Découverte",
      "difficulty.intermediate": "Intermédiaire",
      "difficulty.advanced": "Avancé",
      "difficulty.expert": "Expert",

      "progress.text": "Carte {position} sur {count}",
      "progress.label": "Progression dans cet exercice",

      "loading.title": "Ouverture de votre exercice…",
      "unsupported.title": "Ouvrez ceci depuis Telegram",
      "unsupported.body":
        "Cet exercice s'exécute dans Telegram. Touchez le bouton dans la conversation pour l'ouvrir.",
      "auth.title": "Telegram n'a pas pu vérifier ce lancement",
      "auth.body": "Fermez cette fenêtre et rouvrez l'exercice depuis la conversation.",
      "forbidden.title": "Indisponible pour ce compte",
      "forbidden.body":
        "Ce compte Telegram n'est pas autorisé à utiliser le Studio d'apprentissage ici.",
      "expired.title": "Cette session est terminée",
      "expired.body": "Les sessions sont courtes par conception. Rouvrez l'exercice pour continuer.",
      "notfound.title": "Exercice introuvable",
      "notfound.body": "Il a peut-être été supprimé, ou il appartient à quelqu'un d'autre.",
      "offline.title": "Pas de connexion",
      "offline.body": "Vérifiez votre connexion, puis réessayez.",
      "server.title": "Une erreur est survenue",
      "server.body": "Le Studio d'apprentissage n'a pas pu terminer. Réessayez dans un instant.",
      "throttled.title": "Un instant",
      "throttled.body": "Trop de requêtes. Attendez quelques secondes, puis réessayez.",
      "conflict.title": "Cette carte a déjà avancé",
      "conflict.body": "Rouvrez l'exercice pour reprendre où vous en êtes.",
      "complete.title": "Exercice terminé",
      "complete.body": "Réponses : {answered} sur {count}",
      "complete.score": "Vous avez {correct} bonnes réponses sur {graded} notées.",
      "complete.feedback.excellent": "Excellent travail — presque tout est juste. Continuez ainsi !",
      "complete.feedback.good": "Bien joué ! Quelques erreurs à revoir pour bien consolider.",
      "complete.feedback.mixed": "Les bases sont là — reprenez ce qui a été manqué.",
      "complete.feedback.rough": "Un exercice difficile — revoir la leçon avant de réessayer aidera.",
      "complete.feedback.to_review": "{count} question(s) à revoir.",
      "complete.no_score": "Rien ici n'est corrigé automatiquement. Vos réponses ont été enregistrées.",
      "complete.ungraded":
        "{count} réponse(s) supplémentaire(s) ont été enregistrées mais ne sont pas corrigées automatiquement.",
      "complete.review": "Prochaine révision suggérée : {date}",
      "complete.guidance": "Demandez un autre exercice à tout moment pour continuer à vous entraîner.",
      "complete.correct": "Correct",
      "complete.incorrect": "Incorrect",
      "complete.recorded_item": "Enregistré",

      "action.submit": "Valider",
      "action.continue": "Continuer",
      "action.retry": "Réessayer",
      "action.reopen": "Rouvrir",
      "action.close": "Fermer",

      "feedback.recorded": "Réponse enregistrée.",

      "invalid.required": "Répondez pour continuer.",
      "invalid.choose_one": "Choisissez une option.",
      "invalid.choose_some": "Choisissez au moins une option.",
      "invalid.all_blanks": "Complétez tous les blancs.",
      "invalid.all_pairs": "Associez tous les éléments.",
      "invalid.all_items": "Placez tous les éléments.",
      "invalid.all_markers": "Étiquetez tous les repères.",
      "invalid.all_cells": "Complétez toutes les cases.",
      "invalid.all_steps": "Répondez à chaque étape.",
      "invalid.all_prompts": "Répondez à chaque consigne.",
      "invalid.point": "Touchez l'image pour placer votre repère.",
      "invalid.rating": "Choisissez une note.",
      "invalid.attempt_required": "Écrivez ce dont vous vous souvenez avant de retourner la carte.",
      "invalid.reveal_first": "Retournez la carte avant d'évaluer votre mémoire.",
      "invalid.too_long": "C'est trop long à envoyer. Raccourcissez votre réponse, puis réessayez.",
      "invalid.min_words": "Écrivez au moins ce nombre de mots : {count}",
      "invalid.max_words": "Ne dépassez pas ce nombre de mots : {count}",

      "card.unsupported.title": "Cette carte ne peut pas s'afficher ici",
      "card.unsupported.body":
        "Cette application ne sait pas encore afficher ce type de carte. Demandez-la dans la conversation.",
      "card.unsupported.type": "Type de carte : {type}",

      "card.type.multiple_choice": "Choix multiple",
      "card.type.multi_select": "Plusieurs réponses",
      "card.type.true_false": "Vrai ou faux",
      "card.type.classification": "Classement",
      "card.type.categorization": "Regroupement",
      "card.type.image_choice": "Choisir une image",
      "card.type.scenario_choice": "Scénario",
      "card.type.decision_path": "Parcours de décision",
      "card.type.case_study": "Étude de cas",
      "card.type.fill_blank": "Texte à trous",
      "card.type.short_answer": "Réponse courte",
      "card.type.typed_recall": "Restitution",
      "card.type.free_response": "Réponse rédigée",
      "card.type.rubric_response": "Réponse guidée",
      "card.type.translation": "Traduction",
      "card.type.error_correction": "Correction",
      "card.type.code_response": "Code",
      "card.type.sentence_order": "Ordre des mots",
      "card.type.sequence_order": "Mise en ordre",
      "card.type.timeline": "Chronologie",
      "card.type.process_flow": "Processus",
      "card.type.matching": "Association",
      "card.type.flashcard": "Carte mémoire",
      "card.type.image_observation": "Observation d'image",
      "card.type.diagram": "Schéma",
      "card.type.hotspot": "Point sur l'image",
      "card.type.labeling": "Étiquetage",
      "card.type.table_grid": "Tableau",
      "card.type.confidence_rating": "Confiance",
      "card.type.reflection": "Réflexion",
      "card.type.self_explanation": "Expliquer soi-même",

      "card.true": "Vrai",
      "card.false": "Faux",
      "card.statement": "Cette affirmation est-elle vraie ?",
      "card.multi_hint": "Choisissez toutes les options qui s'appliquent.",
      "card.order_hint": "Mettez ceci en ordre avec les boutons haut et bas.",
      "card.timeline_hint": "Mettez ceci en ordre, du plus ancien au plus récent.",
      "card.categorization_hint": "Choisissez un groupe pour chaque élément.",
      "card.error_correction_hint": "Réécrivez le passage en corrigeant les erreurs.",
      "card.move_up": "Monter",
      "card.move_down": "Descendre",
      "card.position": "Position {position}",
      "card.choose": "Choisir",
      "card.unchosen": "— à choisir —",
      "card.match_for": "Association pour : {text}",
      "card.category_for": "Groupe pour : {text}",
      "card.blank_label": "Blanc : {label}",
      "card.blank_number": "Blanc {number}",
      "card.answer_label": "Votre réponse",
      "card.words": "Mots : {count}",
      "card.min_words": "Au moins ce nombre de mots : {count}",
      "card.max_words": "Au plus ce nombre de mots : {count}",
      "card.code_notice": "Votre code est conservé comme texte. Il n'est jamais exécuté.",
      "card.code_label": "Votre code ({language})",
      "card.focus_points": "À observer :",
      "card.materials": "Documents :",
      "card.requirements": "Exigences :",
      "card.questions": "Questions :",
      "card.prompt_number": "Consigne {number}",
      "card.step_number": "Étape {number}",
      "card.transcript": "Transcription",
      "card.long_description": "Description complète",
      "card.keyboard_alternative": "Sans pointer ni glisser : {text}",
      "card.image_loading": "Chargement de l'image…",
      "card.image_failed": "L'image n'a pas pu être chargée. Sa description est ci-dessous.",
      "card.recall_label": "Écrivez ce dont vous vous souvenez",
      "card.self_rate": "Vous en souveniez-vous ?",
      "card.turn_over": "Retourner la carte",
      "card.back_label": "La réponse",
      "rate.again": "À revoir",
      "rate.hard": "Difficile",
      "rate.good": "Bien",
      "rate.easy": "Facile",
      "card.confidence": "Quelle est votre confiance ?",
      "card.rating_value": "Note {value}",
      "card.hotspot_hint": "Touchez l'endroit sur l'image.",
      "card.hotspot_keyboard": "Ou utilisez les flèches lorsque l'image a le focus : {coarse} % par pas, {fine} % avec Maj.",
      "card.hotspot_selected": "Sélection : {x} % horizontalement, {y} % verticalement",
      "card.hotspot_clear": "Effacer le repère",
      "card.hotspot_pick": "Choisir un endroit sur l'image",
      "card.marker_label": "Repère {number}",
      "card.grid_cell": "{row} — {column}",
    },

    es: {
      "app.title": "Estudio de aprendizaje",
      "app.skip": "Ir al ejercicio",

      "meta.summary": "{minutes} min · {difficulty} · Tarjetas: {count}",
      "difficulty.introductory": "Inicial",
      "difficulty.intermediate": "Intermedio",
      "difficulty.advanced": "Avanzado",
      "difficulty.expert": "Experto",

      "progress.text": "Tarjeta {position} de {count}",
      "progress.label": "Avance en este ejercicio",

      "loading.title": "Abriendo tu ejercicio…",
      "unsupported.title": "Ábrelo desde Telegram",
      "unsupported.body":
        "Este ejercicio se ejecuta dentro de Telegram. Toca el botón del chat para abrirlo.",
      "auth.title": "Telegram no pudo verificar esta apertura",
      "auth.body": "Cierra esta ventana y abre el ejercicio otra vez desde el chat.",
      "forbidden.title": "No disponible para esta cuenta",
      "forbidden.body":
        "Esta cuenta de Telegram no está habilitada para usar el Estudio de aprendizaje aquí.",
      "expired.title": "Esta sesión ha terminado",
      "expired.body": "Las sesiones son cortas por diseño. Vuelve a abrir el ejercicio para seguir.",
      "notfound.title": "Ejercicio no encontrado",
      "notfound.body": "Puede que se haya eliminado, o que pertenezca a otra persona.",
      "offline.title": "Sin conexión",
      "offline.body": "Revisa tu conexión e inténtalo otra vez.",
      "server.title": "Algo ha ido mal",
      "server.body": "El Estudio de aprendizaje no pudo terminar. Inténtalo en un momento.",
      "throttled.title": "Espera un momento",
      "throttled.body": "Demasiadas peticiones. Espera unos segundos e inténtalo otra vez.",
      "conflict.title": "Esa tarjeta ya ha avanzado",
      "conflict.body": "Vuelve a abrir el ejercicio para continuar donde estás.",
      "complete.title": "Ejercicio terminado",
      "complete.body": "Respondidas: {answered} de {count}",
      "complete.score": "Acertaste {correct} de {graded} respuestas corregidas.",
      "complete.feedback.excellent": "¡Excelente trabajo — casi todo correcto! Sigue así.",
      "complete.feedback.good": "¡Bien hecho! Repasa los pocos errores para afianzar lo aprendido.",
      "complete.feedback.mixed": "Las bases están — vuelve a mirar lo que falló.",
      "complete.feedback.rough": "Un ejercicio difícil: repasar la lección antes de reintentarlo ayudará.",
      "complete.feedback.to_review": "{count} pregunta(s) para repasar.",
      "complete.no_score": "Nada aquí se corrige automáticamente. Tus respuestas se registraron.",
      "complete.ungraded":
        "Se registraron {count} respuesta(s) más, pero no se corrigen automáticamente.",
      "complete.review": "Próximo repaso sugerido: {date}",
      "complete.guidance": "Pide otro ejercicio cuando quieras para seguir practicando.",
      "complete.correct": "Correcto",
      "complete.incorrect": "Incorrecto",
      "complete.recorded_item": "Registrado",

      "action.submit": "Enviar",
      "action.continue": "Continuar",
      "action.retry": "Reintentar",
      "action.reopen": "Volver a abrir",
      "action.close": "Cerrar",

      "feedback.recorded": "Respuesta registrada.",

      "invalid.required": "Responde para continuar.",
      "invalid.choose_one": "Elige una opción.",
      "invalid.choose_some": "Elige al menos una opción.",
      "invalid.all_blanks": "Completa todos los huecos.",
      "invalid.all_pairs": "Relaciona todos los elementos.",
      "invalid.all_items": "Coloca todos los elementos.",
      "invalid.all_markers": "Etiqueta todas las marcas.",
      "invalid.all_cells": "Completa todas las celdas.",
      "invalid.all_steps": "Responde a cada paso.",
      "invalid.all_prompts": "Responde a cada consigna.",
      "invalid.point": "Toca la imagen para colocar tu marca.",
      "invalid.rating": "Elige una valoración.",
      "invalid.attempt_required": "Escribe lo que recuerdes antes de dar la vuelta a la tarjeta.",
      "invalid.reveal_first": "Da la vuelta a la tarjeta antes de valorar tu recuerdo.",
      "invalid.too_long": "Es demasiado largo para enviarlo. Acorta tu respuesta e inténtalo otra vez.",
      "invalid.min_words": "Escribe al menos esta cantidad de palabras: {count}",
      "invalid.max_words": "No pases de esta cantidad de palabras: {count}",

      "card.unsupported.title": "Esta tarjeta no se puede mostrar aquí",
      "card.unsupported.body":
        "Esta aplicación todavía no sabe mostrar una tarjeta de este tipo. Pídela en el chat.",
      "card.unsupported.type": "Tipo de tarjeta: {type}",

      "card.type.multiple_choice": "Opción múltiple",
      "card.type.multi_select": "Varias respuestas",
      "card.type.true_false": "Verdadero o falso",
      "card.type.classification": "Clasificar",
      "card.type.categorization": "Agrupar",
      "card.type.image_choice": "Elegir una imagen",
      "card.type.scenario_choice": "Escenario",
      "card.type.decision_path": "Ruta de decisiones",
      "card.type.case_study": "Estudio de caso",
      "card.type.fill_blank": "Rellenar huecos",
      "card.type.short_answer": "Respuesta corta",
      "card.type.typed_recall": "Recuerdo",
      "card.type.free_response": "Respuesta escrita",
      "card.type.rubric_response": "Respuesta guiada",
      "card.type.translation": "Traducción",
      "card.type.error_correction": "Corrección",
      "card.type.code_response": "Código",
      "card.type.sentence_order": "Orden de palabras",
      "card.type.sequence_order": "Poner en orden",
      "card.type.timeline": "Cronología",
      "card.type.process_flow": "Proceso",
      "card.type.matching": "Emparejar",
      "card.type.flashcard": "Tarjeta de memoria",
      "card.type.image_observation": "Observar la imagen",
      "card.type.diagram": "Diagrama",
      "card.type.hotspot": "Punto en la imagen",
      "card.type.labeling": "Etiquetado",
      "card.type.table_grid": "Tabla",
      "card.type.confidence_rating": "Confianza",
      "card.type.reflection": "Reflexión",
      "card.type.self_explanation": "Explícalo tú",

      "card.true": "Verdadero",
      "card.false": "Falso",
      "card.statement": "¿Es verdadera esta afirmación?",
      "card.multi_hint": "Elige todas las opciones que correspondan.",
      "card.order_hint": "Ordénalas con los botones de subir y bajar.",
      "card.timeline_hint": "Ordénalas, de lo más antiguo a lo más reciente.",
      "card.categorization_hint": "Elige un grupo para cada elemento.",
      "card.error_correction_hint": "Reescribe el pasaje con los errores corregidos.",
      "card.move_up": "Subir",
      "card.move_down": "Bajar",
      "card.position": "Posición {position}",
      "card.choose": "Elegir",
      "card.unchosen": "— elegir —",
      "card.match_for": "Pareja de: {text}",
      "card.category_for": "Grupo de: {text}",
      "card.blank_label": "Hueco: {label}",
      "card.blank_number": "Hueco {number}",
      "card.answer_label": "Tu respuesta",
      "card.words": "Palabras: {count}",
      "card.min_words": "Al menos esta cantidad de palabras: {count}",
      "card.max_words": "Como máximo esta cantidad de palabras: {count}",
      "card.code_notice": "Tu código se guarda como texto. Nunca se ejecuta.",
      "card.code_label": "Tu código ({language})",
      "card.focus_points": "Fíjate en:",
      "card.materials": "Materiales:",
      "card.requirements": "Requisitos:",
      "card.questions": "Preguntas:",
      "card.prompt_number": "Consigna {number}",
      "card.step_number": "Paso {number}",
      "card.transcript": "Transcripción",
      "card.long_description": "Descripción completa",
      "card.keyboard_alternative": "Sin apuntar ni arrastrar: {text}",
      "card.image_loading": "Cargando la imagen…",
      "card.image_failed": "No se pudo cargar la imagen. Su descripción está debajo.",
      "card.recall_label": "Escribe lo que recuerdes",
      "card.self_rate": "¿Cómo lo recordaste?",
      "card.turn_over": "Dar la vuelta a la tarjeta",
      "card.back_label": "La respuesta",
      "rate.again": "Otra vez",
      "rate.hard": "Difícil",
      "rate.good": "Bien",
      "rate.easy": "Fácil",
      "card.confidence": "¿Cuánta confianza tienes?",
      "card.rating_value": "Valoración {value}",
      "card.hotspot_hint": "Toca el punto en la imagen.",
      "card.hotspot_keyboard": "O usa las flechas cuando la imagen tenga el foco: {coarse} % por paso, {fine} % con Mayús.",
      "card.hotspot_selected": "Elegido: {x} % a lo ancho, {y} % a lo alto",
      "card.hotspot_clear": "Borrar la marca",
      "card.hotspot_pick": "Elegir un punto en la imagen",
      "card.marker_label": "Marca {number}",
      "card.grid_cell": "{row} — {column}",
    },
  };

  /**
   * The locale whose table will actually be used.
   *
   * `pt-BR` -> `pt` -> fallback. Case and separator are normalised first, so
   * `PT_br` resolves the same way a well-formed tag would; anything that is not
   * a string, or names no table, resolves to the fallback.
   */
  function resolveLocale(requested) {
    if (typeof requested !== "string") {
      return FALLBACK_LOCALE;
    }
    var tag = requested.trim().toLowerCase().replace(/_/g, "-");
    if (Object.prototype.hasOwnProperty.call(STRINGS, tag)) {
      return tag;
    }
    var base = tag.split("-")[0];
    if (Object.prototype.hasOwnProperty.call(STRINGS, base)) {
      return base;
    }
    return FALLBACK_LOCALE;
  }

  function interpolate(template, values) {
    if (!values) {
      return template;
    }
    return template.replace(/\{(\w+)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : whole;
    });
  }

  /**
   * Build a `t(key, values)` for one locale.
   *
   * A key missing from the chosen table falls through to English, and a key
   * missing from English too returns the key itself. Returning the key is
   * deliberate: it renders as something obviously wrong in the interface and
   * obviously findable in the source, where a blank string would look like a
   * layout bug and an exception would take the card down over a caption.
   */
  function translator(requested) {
    var locale = resolveLocale(requested);
    var table = STRINGS[locale];
    var fallback = STRINGS[FALLBACK_LOCALE];

    function t(key, values) {
      var template = Object.prototype.hasOwnProperty.call(table, key)
        ? table[key]
        : Object.prototype.hasOwnProperty.call(fallback, key)
          ? fallback[key]
          : key;
      return interpolate(template, values);
    }

    t.locale = locale;
    return t;
  }

  global.LearningStudioI18n = {
    FALLBACK_LOCALE: FALLBACK_LOCALE,
    STRINGS: STRINGS,
    LOCALES: Object.keys(STRINGS),
    resolveLocale: resolveLocale,
    translator: translator,
  };
})(typeof window !== "undefined" ? window : this);
