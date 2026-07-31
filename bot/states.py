from aiogram.fsm.state import State, StatesGroup


class HomeworkStates(StatesGroup):
    waiting_for_text_or_file = State()


class TransferStates(StatesGroup):
    waiting_for_comment = State()


class BroadcastStates(StatesGroup):
    waiting_for_text = State()


class PhoneAuthStates(StatesGroup):
    waiting_for_phone = State()


class DateRangeStates(StatesGroup):
    waiting_for_date_from = State()
    waiting_for_date_to = State()


class ManagerSummaryStates(StatesGroup):
    waiting_for_date_from = State()
    waiting_for_date_to = State()