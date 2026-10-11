---
topics:
- infrastructure
- config
- general
symptoms:
- get_system_health מחזיר duplicate_instance_nodes עם רוב ה-nodes, ו-row_count גדול
  מ-node_count
- אותו node מופיע פעמיים עם uptime שונה מאוד (למשל 30s מול 26690s) באותה דגימה
- get_process_table מציג שתי קבוצות PID נפרדות עם פקודת הרצה זהה ואותו קובץ קונפיג
- 'המופע הוותיק DEGRADED עם connection_errors/drops גבוהים בעוד המופע החדש HEALTHY
  עם drops: 0'
- שדות total/running בטבלת התהליכים נמוכים ממספר התהליכים המפורטים בפועל
---

# שתי הרצות מקבילות של אותו קונפיג — קבוצות PID כפולות לאותם nodes

## Root cause

הופעלה הרצה שנייה של ה-launcher עם אותו קובץ קונפיג מבלי לעצור את ההרצה הקודמת, כך שכל node רץ בשני עותקים המתחרים על אותם משאבי חיישן/רשת.

## Checks (read-only)

    get_process_table — לזהות שתי קבוצות PID עם cmd זהה ו-uptime_s שונה
    get_system_health — לבדוק duplicate_instance_nodes ולהשוות מדדי שגיאות בין המופעים
    ps/pgrep ידני על שמות הבינאריים והמודולים כדי לאשר את מספר התהליכים בפועל
    get_ecal_topology — לבדוק publishers/subscribers כפולים על אותם נושאים
    יומני ה-launcher — לאתר את זמן ומקור ההפעלה השנייה

## Fix

לאשר מול המפעיל איזו הרצה היא הרצויה, לעצור בצורה מסודרת את ההרצה המיותרת דרך ה-launcher (ולא kill ישיר), ולוודא ב-get_process_table שנותרה קבוצת PID אחת בלבד.
