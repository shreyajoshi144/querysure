"""Fixed evaluation corpus for retrieval: 8 company documents and 100 questions with gold answers.

A question is *relevant-retrieved* when a returned passage comes from the gold document AND contains the gold marker text.
``keyword`` questions share vocabulary with the answer, ``paraphrase`` questions deliberately use different words (the case where
lexical retrieval is expected to be weakest), ``offtopic`` questions have no answer in the corpus and should retrieve nothing.
"""
DOCS = {
    "expenses_policy.md": """# Travel and expenses

Employees travelling on company business are reimbursed for reasonable costs. The meal per diem is 60 dollars per day, and alcohol is never reimbursed. Hotels are capped at 220 dollars per night in standard cities. Flights must be economy class for journeys under six hours; premium cabins need written approval from a director.

# Claims

Submit receipts within 30 days of the trip through the expense tool. Claims above 1,000 dollars need director approval before payment. Mileage for personal cars is paid at 0.67 dollars per mile. The corporate card must be used for flights and hotels, and personal purchases on it are repaid within 14 days.
""",
    "leave_policy.md": """# Annual leave

Full-time employees receive 25 days of annual leave per year. Unused leave can be carried over, up to a maximum of 5 days, and the carry-over must be used by March 31 or it lapses. Leave requests are approved by the line manager at least two weeks ahead for periods longer than five days.

# Other leave

Sick leave is 10 days per year; a medical certificate is required after 3 consecutive days. Parental leave is 16 weeks at full pay for either parent. Bereavement leave is 5 days for an immediate family member. Unpaid leave needs approval from the department head and HR.
""",
    "it_security.md": """# Accounts and devices

Passwords must be at least 14 characters and are never shared. Multi-factor authentication is required for every account. Laptops are encrypted and receive operating system patches automatically; critical security patches must be installed within 14 days. USB storage devices are blocked on company laptops.

# Incidents

A lost or stolen device must be reported to the security desk within 24 hours so it can be wiped remotely. Use the VPN on public wifi. Suspected phishing emails are reported with the report button and never forwarded to colleagues.
""",
    "onboarding.md": """# First weeks

New hires are paired with a buddy who answers day-to-day questions during the first month. Equipment is ordered by IT before the start date, and system access is requested through a ticket by the hiring manager. The probation period is 90 days, with a review meeting at day 45 and day 90.

# Training

Security awareness training must be completed within 14 days of joining. Product training modules are scheduled during the first three weeks, and the manager agrees a 30-60-90 day plan with the new hire in week one.
""",
    "product_returns.md": """# Returns

Customers may return unopened items within 30 days of delivery for a full refund. Opened items can be returned within 14 days and incur a 15 percent restocking fee. Gift cards are not refundable. For international orders the customer pays return shipping.

# Refunds and warranty

Refunds are issued to the original payment method within 5 to 7 business days after the returned item is inspected. Defective products are covered by a warranty of 24 months from purchase and are repaired or replaced free of charge.
""",
    "support_sla.md": """# Response times

Severity 1 incidents (service fully down) get a response within 30 minutes, around the clock. Severity 2 incidents are answered within 4 business hours and severity 3 requests within 1 business day. Unresolved severity 1 incidents are escalated to the on-call engineering manager after two hours.

# Availability

Planned maintenance happens on Sundays between 02:00 and 04:00 UTC and is announced on the status page. If monthly uptime falls below 99.9 percent, customers receive a service credit of 5 percent of the monthly fee.
""",
    "data_retention.md": """# Retention periods

Invoices and financial records are kept for 7 years. Application logs are kept for 90 days. Encrypted backups are retained for 35 days. Analytics data is anonymised after 13 months. A legal hold suspends deletion of the affected records until released by the legal team.

# Deletion requests

Customers can ask for their personal data to be deleted; requests are completed within 30 days and confirmed in writing. Data needed for legal obligations is kept and the customer is told which data and why.
""",
    "procurement.md": """# Approvals

Purchases under 500 dollars need only manager approval. Purchases from 500 to 5,000 dollars require two quotes. Purchases above 5,000 dollars require three quotes and approval by the procurement committee. Software subscriptions follow the same limits based on the total annual cost.

# Vendors

New vendors pass a security review before onboarding. Contracts are signed only by the legal department. Preferred vendors are listed on the intranet, and standard payment terms are net 45 days.
""",
}

# (class, question, gold document, gold marker -- lower-case text that must appear in the retrieved passage)
QUESTIONS = [
    ("keyword", "What is the hotel cap per night for travel?", "expenses_policy.md", "220"),
    ("keyword", "How many days of annual leave do employees get?", "leave_policy.md", "25 days"),
    ("keyword", "How long do I have to submit expense receipts?", "expenses_policy.md", "30 days"),
    ("keyword", "What is the minimum password length?", "it_security.md", "14 characters"),
    ("keyword", "How fast must a lost device be reported?", "it_security.md", "24 hours"),
    ("keyword", "What is the restocking fee for opened items?", "product_returns.md", "15 percent"),
    ("keyword", "How long is the warranty for defective products?", "product_returns.md", "24 months"),
    ("keyword", "What is the response time for a severity 1 incident?", "support_sla.md", "30 minutes"),
    ("keyword", "When is the planned maintenance window?", "support_sla.md", "sundays"),
    ("keyword", "How long are application logs kept?", "data_retention.md", "90 days"),
    ("keyword", "How many quotes are needed for purchases above 5,000 dollars?", "procurement.md", "three quotes"),
    ("keyword", "How long is the probation period?", "onboarding.md", "90 days"),
    ("keyword", "How many weeks of parental leave are offered?", "leave_policy.md", "16 weeks"),
    ("keyword", "What are the standard payment terms for vendors?", "procurement.md", "net 45"),
    ("paraphrase", "Can I get my money back if I changed my mind about something I bought?", "product_returns.md", "full refund"),
    ("paraphrase", "How much will the company pay for my dinner on a business trip?", "expenses_policy.md", "per diem"),
    ("paraphrase", "Am I allowed to roll over unused vacation into next year?", "leave_policy.md", "carried over"),
    ("paraphrase", "What should I do if someone steals my work laptop?", "it_security.md", "24 hours"),
    ("paraphrase", "Who has to sign off before I buy a software subscription for 3000 dollars?", "procurement.md", "two quotes"),
    ("paraphrase", "How quickly will someone respond if the whole system goes down?", "support_sla.md", "30 minutes"),
    ("paraphrase", "Do you keep my personal information forever?", "data_retention.md", "deleted"),
    ("paraphrase", "What happens during my first months after joining?", "onboarding.md", "buddy"),
    ("paraphrase", "Is there compensation when the service is unavailable too often?", "support_sla.md", "service credit"),
    ("paraphrase", "Can I plug in a thumb drive?", "it_security.md", "usb"),
    ("paraphrase", "How many days off do I get when a relative dies?", "leave_policy.md", "bereavement"),
    ("paraphrase", "Will I be paid back for flying business class?", "expenses_policy.md", "economy"),
    ("offtopic", "What is the capital of France?", None, None),
    ("offtopic", "Who won the football match yesterday?", None, None),
    ("offtopic", "How do I bake sourdough bread?", None, None),
    ("offtopic", "What is the boiling point of water?", None, None),
    ("offtopic", "Explain quantum entanglement.", None, None),
    ("offtopic", "Best hiking trails in Norway?", None, None),
    ("offtopic", "What will the weather be in Tokyo tomorrow?", None, None),
    ("offtopic", "How tall is Mount Everest?", None, None),
    ("keyword", 'What is the mileage rate for personal cars?', 'expenses_policy.md', '0.67'),
    ("keyword", 'How many days of sick leave are there per year?', 'leave_policy.md', '10 days'),
    ("keyword", 'How many days of bereavement leave are given?', 'leave_policy.md', '5 days'),
    ("keyword", 'Are USB storage devices allowed on company laptops?', 'it_security.md', 'usb'),
    ("keyword", 'How many days do I have to complete security awareness training?', 'onboarding.md', '14 days'),
    ("keyword", 'How long are invoices and financial records kept?', 'data_retention.md', '7 years'),
    ("keyword", 'What is the service credit when uptime falls below 99.9 percent?', 'support_sla.md', '5 percent'),
    ("keyword", 'Are gift cards refundable?', 'product_returns.md', 'gift cards'),
    ("keyword", 'What is the maximum carry-over of annual leave?', 'leave_policy.md', '5 days'),
    ("keyword", 'What is the hotel limit for standard cities?', 'expenses_policy.md', '220'),
    ("paraphrase", 'Can my employer cover a flight in a fancier cabin?', 'expenses_policy.md', 'premium cabins'),
    ("paraphrase", 'How do I get paid for driving my own vehicle for work?', 'expenses_policy.md', 'mileage'),
    ("paraphrase", 'Will I be reimbursed for beer with dinner on the road?', 'expenses_policy.md', 'alcohol'),
    ("paraphrase", 'What must I do if I put a private purchase on the company plastic?', 'expenses_policy.md', 'repaid'),
    ("paraphrase", 'Who signs off on big travel claims?', 'expenses_policy.md', 'director approval'),
    ("paraphrase", 'How far ahead should I ask my boss for a long holiday?', 'leave_policy.md', 'two weeks'),
    ("paraphrase", "When do I need a doctor's note for being ill?", 'leave_policy.md', 'medical certificate'),
    ("paraphrase", 'What is the deadline for using the days I rolled over?', 'leave_policy.md', 'march 31'),
    ("paraphrase", 'How long can new parents stay home?', 'leave_policy.md', 'parental'),
    ("paraphrase", 'Who must approve a period without pay?', 'leave_policy.md', 'department head'),
    ("paraphrase", 'How many days can I be off sick in a year?', 'leave_policy.md', '10 days'),
    ("paraphrase", 'Is it fine to tell a coworker my login?', 'it_security.md', 'never shared'),
    ("paraphrase", 'What second step beyond a password do I need to sign in?', 'it_security.md', 'multi-factor'),
    ("paraphrase", 'How soon must urgent security updates be applied?', 'it_security.md', '14 days'),
    ("paraphrase", 'What should I do with a suspicious email?', 'it_security.md', 'phishing'),
    ("paraphrase", 'Is it safe to work from a cafe?', 'it_security.md', 'vpn'),
    ("paraphrase", 'Is the hard drive of my laptop protected if it is lost?', 'it_security.md', 'encrypted'),
    ("paraphrase", 'Who orders my computer before I start?', 'onboarding.md', 'equipment'),
    ("paraphrase", 'How do I get access to the systems I need?', 'onboarding.md', 'ticket'),
    ("paraphrase", 'When are my check-ins during the trial period?', 'onboarding.md', 'day 45'),
    ("paraphrase", 'How soon must I finish the mandatory cyber awareness course?', 'onboarding.md', 'security awareness'),
    ("paraphrase", 'Is there somebody assigned to help me settle in?', 'onboarding.md', 'buddy'),
    ("paraphrase", 'What plan do I agree with my manager in the first week?', 'onboarding.md', '30-60-90'),
    ("paraphrase", 'How long is the trial employment period?', 'onboarding.md', '90 days'),
    ("paraphrase", 'Can I send back a present card I did not want?', 'product_returns.md', 'gift cards'),
    ("paraphrase", 'Who pays postage when sending back an order from abroad?', 'product_returns.md', 'international'),
    ("paraphrase", 'How long until the money lands back on my card after I return something?', 'product_returns.md', '5 to 7'),
    ("paraphrase", 'What if I opened the box before deciding to return it?', 'product_returns.md', 'restocking'),
    ("paraphrase", 'Does the manufacturer fix faulty goods at no charge?', 'product_returns.md', 'defective'),
    ("paraphrase", 'How soon do you reply to a medium-priority ticket?', 'support_sla.md', '4 business hours'),
    ("paraphrase", 'When does a long-running outage get escalated to management?', 'support_sla.md', 'two hours'),
    ("paraphrase", 'Where do I find out about scheduled downtime?', 'support_sla.md', 'status page'),
    ("paraphrase", 'How much money back do I get for poor availability?', 'support_sla.md', 'service credit'),
    ("paraphrase", 'What is the target for low-priority requests?', 'support_sla.md', '1 business day'),
    ("paraphrase", 'How long do you keep billing records?', 'data_retention.md', '7 years'),
    ("paraphrase", 'For how many days are copies of the database stored?', 'data_retention.md', '35 days'),
    ("paraphrase", 'When does usage statistics data stop identifying people?', 'data_retention.md', 'anonymised'),
    ("paraphrase", 'What stops the deletion of records during a lawsuit?', 'data_retention.md', 'legal hold'),
    ("paraphrase", 'How fast do you erase my account information if I ask?', 'data_retention.md', '30 days'),
    ("paraphrase", 'Why might some of my data not be deleted even after I ask?', 'data_retention.md', 'legal obligations'),
    ("paraphrase", 'What approvals does a 300 dollar purchase need?', 'procurement.md', 'manager approval'),
    ("paraphrase", 'How many offers must I collect for a 2,000 dollar expense?', 'procurement.md', 'two quotes'),
    ("paraphrase", 'Who can sign supplier agreements?', 'procurement.md', 'legal department'),
    ("paraphrase", 'Where can I see approved suppliers?', 'procurement.md', 'intranet'),
    ("paraphrase", 'What checks does a new supplier go through?', 'procurement.md', 'security review'),
    ("paraphrase", 'How is a yearly software licence judged against the limits?', 'procurement.md', 'annual cost'),
    ("paraphrase", 'How many days do we have to pay suppliers?', 'procurement.md', 'net 45'),
    ("paraphrase", 'What committee approves very large purchases?', 'procurement.md', 'procurement committee'),
    ("offtopic", 'How many calories are in an avocado?', None, None),
    ("offtopic", 'Who painted the Mona Lisa?', None, None),
    ("offtopic", 'How do I change a flat tire?', None, None),
    ("offtopic", 'What year did the Berlin Wall fall?', None, None),
    ("offtopic", 'Recommend a good science fiction novel.', None, None),
    ("offtopic", 'How far is the moon from earth?', None, None),
    ("offtopic", 'What is the exchange rate of yen to dollars?', None, None),
    ("offtopic", 'How do vaccines work?', None, None),
]

# Stated BEFORE measuring: a vector index is only worth its cost if lexical retrieval misses by more than this.
VECTOR_INDEX_THRESHOLDS = {"keyword_recall_at_3_min": 0.90, "paraphrase_recall_at_3_min": 0.60, "offtopic_false_positive_max": 0.25}
